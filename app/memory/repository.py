from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.memory.types import MemoryMessage, MemoryUpdate
from app.models import Conversation, ConversationSummary, Message, UserMemory, utc_now
from app.services.jobs import enqueue_tool_job
from app.services.privacy import redact_pii


class MemoryConflict(RuntimeError):
    pass


class ConversationBusy(RuntimeError):
    pass


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_summary(db, conversation_id: int) -> ConversationSummary:
    existing = db.get(ConversationSummary, conversation_id, populate_existing=True)
    if existing is not None:
        return existing
    values = dict(conversation_id=conversation_id, summary_json="[]", version=0,
                  summarized_until_message_id=0, extracted_until_message_id=0, updated_at=utc_now())
    if db.get_bind().dialect.name == "mysql":
        statement = mysql_insert(ConversationSummary).values(**values)
        db.execute(statement.on_duplicate_key_update(conversation_id=statement.inserted.conversation_id))
    else:
        db.execute(sqlite_insert(ConversationSummary).values(**values).on_conflict_do_nothing())
    return db.get(ConversationSummary, conversation_id, populate_existing=True)


def acquire_turn(db, conversation_id: int, seconds: int) -> str:
    ensure_summary(db, conversation_id)
    token = str(uuid.uuid4())
    now = utc_now()
    claimed = db.execute(update(ConversationSummary).where(
        ConversationSummary.conversation_id == conversation_id,
        or_(ConversationSummary.turn_token.is_(None), ConversationSummary.turn_expires_at <= now),
    ).values(turn_token=token, turn_expires_at=now + timedelta(seconds=seconds)))
    if claimed.rowcount != 1:
        db.rollback()
        raise ConversationBusy("another turn is running")
    # An expired owner may have left a generating message behind. It must never
    # subsequently commit after its lease has been fenced out.
    db.execute(update(Message).where(Message.conversation_id == conversation_id,
                                     Message.status == "generating").values(status="failed"))
    db.commit()
    return token


def assert_turn(db, conversation_id: int, token: str, seconds: int) -> None:
    result = db.execute(update(ConversationSummary).where(
        ConversationSummary.conversation_id == conversation_id,
        ConversationSummary.turn_token == token,
        ConversationSummary.turn_expires_at > utc_now(),
    ).values(turn_expires_at=utc_now() + timedelta(seconds=seconds)))
    if result.rowcount != 1:
        raise ConversationBusy("turn lease expired")


def release_turn(db, conversation_id: int, token: str) -> None:
    db.execute(update(ConversationSummary).where(
        ConversationSummary.conversation_id == conversation_id,
        ConversationSummary.turn_token == token,
    ).values(turn_token=None, turn_expires_at=None))
    db.commit()


def to_message(row: Message) -> MemoryMessage:
    return MemoryMessage(row.public_id, row.sender_type,
                         redact_pii(row.redacted_content or row.content),
                         row.created_at.isoformat(), db_id=row.id)


def snapshot_upper(db, conversation_id: int, before_id: int | None = None) -> int:
    # Never jump over an unfinished older message; integer IDs need not be contiguous.
    generating = db.scalar(select(func.min(Message.id)).where(
        Message.conversation_id == conversation_id, Message.status == "generating"))
    bounds = [value for value in (before_id, generating) if value is not None]
    statement = select(func.max(Message.id)).where(
        Message.conversation_id == conversation_id, Message.status == "completed")
    if bounds:
        statement = statement.where(Message.id < min(bounds))
    return db.scalar(statement) or 0


def window_query(conversation_id: int, boundary: int, upper: int):
    return select(Message).where(Message.conversation_id == conversation_id,
                                 Message.status == "completed", Message.id > boundary,
                                 Message.id <= upper)


def read_window(db, conversation_id: int, boundary: int, upper: int, limit: int, *, latest=False):
    query = window_query(conversation_id, boundary, upper)
    rows = list(db.scalars(query.order_by(Message.id.desc() if latest else Message.id.asc()).limit(limit)))
    return [to_message(row) for row in (reversed(rows) if latest else rows)]


def source_ids(memory: UserMemory | dict) -> set[str]:
    evidence = json.loads(memory.evidence_json) if isinstance(memory, UserMemory) else memory.get("evidence", [])
    return {item["message_id"] for item in evidence if item.get("message_id")}


def memory_dict(row: UserMemory) -> dict:
    evidence = json.loads(row.evidence_json)
    history = [f"{e.get('at', '')} [{e.get('subject', 'uncertain')}/{e.get('stage', 'reported')}] 原话：{e['quote']}"
               for e in evidence if e.get("quote") and not e.get("invalidated")]
    return {"memory_id": row.public_id, "content": row.content, "type": row.type,
            "topics": json.loads(row.topics_json), "evidence": evidence,
            "context_content": "\n".join(history) if history else row.content,
            "version": row.version, "status": row.status, "updated_at": row.updated_at.isoformat()}


def enqueue_index(db, row: UserMemory) -> None:
    enqueue_tool_job(db, job_type="MEMORY_INDEX_SYNC",
                     idempotency_key=f"memory-index:{row.public_id}:{row.version}",
                     payload={"memory_id": row.public_id, "student_id": row.student_id,
                              "version": row.version}, priority=5)


def commit_update(db, conversation, version, result, allowed, **kwargs):
    from app.memory.facts import commit_facts
    return commit_facts(db, conversation, version, result, allowed, **kwargs)
