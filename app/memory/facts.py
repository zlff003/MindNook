"""Durable facts and deterministic summaries. No generated summary owns state."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select, update

from app.models import Conversation, ConversationSummary, MemoryEvent, Message, UserMemory, utc_now
from app.memory.repository import MemoryConflict, dumps, enqueue_index, memory_dict
from app.services.privacy import redact_pii


def project_summary(db, conversation_id, boundary, max_chars=400):
    rows = list(db.scalars(select(UserMemory).join(Conversation, Conversation.student_id == UserMemory.student_id)
                .where(Conversation.id == conversation_id, UserMemory.status == "active")
                .order_by(UserMemory.updated_at.desc(), UserMemory.id.desc())))
    items, used = [], 0
    for row in rows:
        evidence = [e for e in json.loads(row.evidence_json) if not e.get("invalidated")]
        if not evidence or any(e.get("conversation_id") != conversation_id or e.get("db_id", boundary + 1) > boundary
                               for e in evidence):
            continue
        text = row.content
        # Skip oversized facts instead of cutting negation/attribution out of a quote.
        if used + len(text) + 1 > max_chars:
            continue
        items.append({"kind": "preference" if row.type == "preference" else "feedback" if row.type == "support_feedback"
                      else "concern", "text": text, "source_message_ids": [e["message_id"] for e in evidence],
                      "memory_id": row.public_id})
        used += len(text) + 1
        if len(items) == 12:
            break
    return list(reversed(items))


def commit_facts(db, conversation, version, result, allowed, *, boundary=None, extracted_until=None,
                 replace_summary=True, mode="extract", verified_targets=(), summary_max_chars=400):
    """Validate again at the write boundary; facts, events, cursors and outbox share one transaction."""
    from app.memory.summary import SummaryGenerator
    targets = list(db.scalars(select(UserMemory).where(UserMemory.student_id == conversation.student_id,
                    UserMemory.public_id.in_([op.memory_id for op in result.memory_operations if op.memory_id]))))
    actual = dict(db.execute(select(Message.public_id, Message).join(Conversation,
                  Conversation.id == Message.conversation_id).where(Conversation.student_id == conversation.student_id,
                  Message.public_id.in_(allowed))).all())
    if actual.keys() != allowed.keys():
        raise ValueError("foreign or missing evidence at commit")
    SummaryGenerator.validate(result, actual, targets, mode=mode)
    if any(op.action != "add" and op.memory_id not in verified_targets for op in result.memory_operations):
        raise ValueError("correction lacks independent verification")
    state = db.get(ConversationSummary, conversation.id, populate_existing=True)
    if state is None or state.version != version:
        raise MemoryConflict("summary changed during generation")
    if boundary is not None and boundary < state.summarized_until_message_id:
        raise MemoryConflict("summary cursor cannot move backwards")
    if extracted_until is not None and extracted_until < state.extracted_until_message_id:
        raise MemoryConflict("extraction cursor cannot move backwards")
    count = db.execute(update(ConversationSummary).where(ConversationSummary.conversation_id == conversation.id,
                       ConversationSummary.version == version).values(version=version + 1, updated_at=utc_now()))
    if count.rowcount != 1:
        raise MemoryConflict("concurrent fact commit")
    changed_ids, replaced_texts = set(), set()
    for op in result.memory_operations:
        source = actual[op.source_message_ids[0]]
        if mode == "correct" and (source.sender_type != "student" or source.conversation_id != conversation.id
                                  or source.status not in ("generating", "completed")):
            raise ValueError("correction source is not an eligible student turn")
        if mode != "correct" and (source.status != "completed" or source.conversation_id != conversation.id):
            raise ValueError("extraction source must be completed in this conversation")
        if mode != "correct" and extracted_until is not None and source.id > extracted_until:
            raise ValueError("source is beyond batch boundary")
        evidence = {"message_id": source.public_id, "db_id": source.id, "conversation_id": source.conversation_id,
                    "role": source.sender_type, "at": source.created_at.isoformat(), "stage": op.stage,
                    "change": op.change, "quote": op.quote, "subject": op.subject}
        before = {}
        if op.action == "add":
            # Type changes cannot resurrect the same retired fact; other quotes from the message remain valid.
            key = hashlib.sha256(dumps([source.public_id, op.quote]).encode()).hexdigest()
            row = db.scalar(select(UserMemory).where(UserMemory.student_id == conversation.student_id,
                                                     UserMemory.dedup_key == key))
            if row:
                continue
            row = UserMemory(student_id=conversation.student_id, content=op.quote, type=op.type,
                             topics_json=dumps(op.topics), evidence_json=dumps([evidence]), dedup_key=key,
                             status="active", version=1, indexed_version=0)
            db.add(row)
            db.flush()
        else:
            row = next(r for r in targets if r.public_id == op.memory_id)
            before = memory_dict(row)
            replaced_texts.add(row.content)
            previous = json.loads(row.evidence_json)
            latest_source = max((e.get("db_id", 0) for e in previous if not e.get("invalidated")), default=0)
            if source.id <= latest_source:
                raise MemoryConflict("an older correction cannot overwrite a newer fact")
            if op.change == "correction":
                previous = [dict(e, invalidated=True) for e in previous]
            values = dict(content=op.quote, type=op.type, topics_json=dumps(op.topics),
                          evidence_json=dumps(previous + [evidence]), version=row.version + 1,
                          status="retracted" if op.action == "retract" else "active", updated_at=utc_now())
            count = db.execute(update(UserMemory).where(UserMemory.id == row.id,
                               UserMemory.version == op.expected_version).values(**values))
            if count.rowcount != 1:
                raise MemoryConflict("fact changed during correction")
            db.refresh(row)
            changed_ids.add(row.public_id)
        db.add(MemoryEvent(event_key=f"fact:{row.public_id}:{row.version}", student_id=conversation.student_id,
                           conversation_id=conversation.id, source_message_id=source.public_id, memory_id=row.public_id,
                           relation=op.change, quote=op.quote, before_json=dumps(before), after_json=dumps(memory_dict(row))))
        enqueue_index(db, row)
    db.flush()
    # Only target facts are invalidated in other summaries, never their entire source message.
    if changed_ids:
        summaries = db.scalars(select(ConversationSummary).join(Conversation,
            Conversation.id == ConversationSummary.conversation_id).where(Conversation.student_id == conversation.student_id))
        for other in summaries:
            old = json.loads(other.summary_json)
            kept = [item for item in old if item.get("memory_id") not in changed_ids
                    and item.get("text") not in replaced_texts]
            # Legacy projections without IDs are conservatively rebuilt from verified facts below/on migration.
            if kept != old:
                revision = {"summary_json": dumps(kept)}
                if other.conversation_id != conversation.id:
                    revision["version"] = other.version + 1
                count = db.execute(update(ConversationSummary).where(
                    ConversationSummary.conversation_id == other.conversation_id,
                    ConversationSummary.version == other.version).values(**revision))
                if count.rowcount != 1:
                    raise MemoryConflict("another summary changed during correction")
    values = {}
    if boundary is not None:
        values["summarized_until_message_id"] = boundary
    if extracted_until is not None:
        values["extracted_until_message_id"] = extracted_until
    if replace_summary:
        values["summary_json"] = dumps(project_summary(db, conversation.id,
            boundary if boundary is not None else state.summarized_until_message_id, summary_max_chars))
    if values:
        db.execute(update(ConversationSummary).where(ConversationSummary.conversation_id == conversation.id).values(**values))
