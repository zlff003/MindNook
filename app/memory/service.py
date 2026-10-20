from __future__ import annotations

import json
import uuid
from datetime import timedelta

from sqlalchemy import select, update

from app.memory.cache import ContextCompressor, ShortTermMemoryService as RecentMessageCache
from app.memory.corrections import needs_reconcile, related_records
from app.memory.batches import claim_batch, finish_batch, pending_correction
from app.memory.index import MemoryIndex
from app.memory.history import raw_history, stable_preferences
from app.memory.repository import MemoryConflict, commit_update, dumps, ensure_summary, read_window, snapshot_upper, window_query
from app.memory.retrieval import recall
from app.memory.summary import SummaryGenerator
from app.memory.types import MemoryContext
from app.models import Conversation, ConversationSummary, MemoryBatch, MemoryEvent, Message, ToolJob, UserMemory, utc_now
from app.services.context_budget import estimate_tokens, input_limit
from app.services.jobs import enqueue_tool_job
from app.services.model import MockModelProvider
from app.services.privacy import redact_pii


class MemoryService(RecentMessageCache):
    """SQL owns durable state; Redis/Chroma are rebuildable accelerators.

    Updates CAS a captured version and commit summary, boundary, memory bodies and
    index outbox together. No write transaction is held across a model call.
    """

    def __init__(self, settings, redis_store, provider=None, *, index=None):
        super().__init__(settings, redis_store)
        self.provider = provider or MockModelProvider()
        self.generator = SummaryGenerator(settings, self.provider)
        self.index = index or MemoryIndex(settings)

    def _history_budget(self, current_input):
        models = [self.settings.model_name, self.settings.model_understanding_name,
                  self.settings.model_safety_name, self.settings.model_response_name]
        available = min(input_limit(self.settings, model) for model in models if model)
        return max(0, min(self.settings.memory_history_tokens,
                          available - self.settings.model_context_reserve_tokens
                          - estimate_tokens(current_input) - self.settings.memory_recall_tokens
                          - self.settings.memory_preference_tokens - self.settings.memory_raw_recall_tokens
                          - self.settings.memory_correction_tokens))

    @staticmethod
    def _items(summary, *, include_disputed=False):
        items = json.loads(summary.summary_json)
        return items if include_disputed else [item for item in items if not item.get("disputed")]

    def _load_window(self, db, conversation, boundary, upper):
        ids = list(db.scalars(window_query(conversation.id, boundary, upper).with_only_columns(Message.id)
                              .order_by(Message.id.asc())))
        cached = self._read_cache(self.key(conversation.public_id)) or []
        by_id = {item.db_id: item for item in cached}
        hit = bool(ids) and all(msg_id in by_id for msg_id in ids)
        if hit:
            messages = [by_id[msg_id] for msg_id in ids[:self.settings.memory_page_size]]
        else:
            messages = read_window(db, conversation.id, boundary, upper, self.settings.memory_page_size)
            tail = read_window(db, conversation.id, 0, upper, self.settings.short_term_memory_max_messages, latest=True)
            self._replace_cache(self.key(conversation.public_id), tail)
        return messages, len(ids), hit

    def _run_update(self, db, conversation, summary, batch, *, mode, focus="", current=None, background=False):
        version = summary.version
        if background and mode != "correct" and len(batch) > 2:
            parent_key = f"v2:{conversation.id}:{mode}:{batch[0].db_id}:{batch[-1].db_id}"
            parent = db.scalar(select(MemoryBatch).where(MemoryBatch.batch_key == parent_key,
                                       MemoryBatch.status.in_(("failed", "split"))))
            if parent is not None:
                parent.status = "split"
                db.commit()
                size = max(2, len(batch) // 2)
                if size > 2 and batch[size - 1].role == "student":
                    size -= 1
                batch = batch[:size]
        related = related_records(db, conversation.student_id, self._items(summary, include_disputed=True), batch, focus,
                                  before_id=current.id if current else None) if mode == "correct" else []
        first, last = (current.id, current.id) if current is not None else (batch[0].db_id, batch[-1].db_id)
        token = claim_batch(db, self.settings, conversation.id, mode, first, last, background=background)
        if token is None:
            return {"purpose": f"memory_{mode}", "status": "already_committed"}
        call = enqueue_tool_job(db, job_type="MEMORY_CALL", idempotency_key=f"memory-call:{uuid.uuid4()}",
                                payload={"conversation_id": conversation.id, "mode": mode, "version": version,
                                         "through": batch[-1].db_id if batch else 0}, priority=0)
        call.status, call.locked_at, call.locked_by = "RUNNING", utc_now(), "memory-service"
        call_id = call.id
        db.commit()
        usage = {"purpose": f"memory_{mode}", "status": "failed", "input_tokens": 0, "output_tokens": 0}
        try:
            unextracted = [m for m in batch if m.db_id > summary.extracted_until_message_id]
            if mode != "correct" and not unextracted:
                from app.memory.types import MemoryUpdate
                result, allowed, usage = MemoryUpdate(), {}, {"purpose": "memory_projection", "status": "succeeded"}
            else:
                result, allowed, usage = self.generator.generate(db, conversation, summary,
                    batch if mode == "correct" else unextracted, related, mode=mode, current=current)
            db.commit()
            commit_update(db, conversation, version, result, allowed,
                          boundary=batch[-1].db_id if mode == "compact" else None,
                          extracted_until=max(summary.extracted_until_message_id, batch[-1].db_id) if mode != "correct" else None,
                          replace_summary=mode != "extract", mode=mode,
                          verified_targets=usage.get("verified_targets", []),
                          summary_max_chars=self.settings.memory_summary_max_chars)
            partial = bool(usage.get("unresolved_targets"))
            if current is not None and not partial:
                db.execute(update(MemoryEvent).where(MemoryEvent.event_key == f"correction:{current.public_id}")
                           .values(status="committed"))
            elif current is None:
                for source in allowed.values():
                    if source.sender_type == "student" and needs_reconcile(source.redacted_content or source.content):
                        pending_correction(db, conversation, source)
                        enqueue_tool_job(db, job_type="MEMORY_RECONCILE",
                            idempotency_key=f"memory-correct:{source.public_id}",
                            payload={"conversation_id": conversation.id, "message_id": source.id}, priority=20)
            finish_batch(db, token, partial=partial,
                         error="unresolved targets: " + ",".join(usage["unresolved_targets"]) if partial else "",
                         retry_seconds=self.settings.memory_batch_retry_seconds)
            db.execute(update(ToolJob).where(ToolJob.id == call_id).values(
                status="SUCCESS", result_json=dumps(usage), locked_at=None, locked_by=None))
            db.commit()
            return usage
        except Exception as exc:
            db.rollback()
            usage = dict(getattr(exc, "memory_usage", usage), status="failed", error=type(exc).__name__)
            finish_batch(db, token, error=str(exc), retry_seconds=self.settings.memory_batch_retry_seconds)
            db.execute(update(ToolJob).where(ToolJob.id == call_id).values(
                status="DEAD", result_json=dumps(usage), last_error=type(exc).__name__, locked_at=None, locked_by=None))
            db.commit()
            setattr(exc, "memory_usage", usage)
            raise

    def _batch(self, messages, *, keep):
        candidates = messages[:-keep] if keep else messages
        result, cost = [], 0
        for item in candidates:
            if len(result) >= self.settings.memory_batch_max_messages:
                break
            next_cost = estimate_tokens(item.content) + 160
            if cost + next_cost > self.settings.memory_batch_tokens:
                break
            result.append(item)
            cost += next_cost
        if len(result) > 1 and result[-1].role == "student":
            result.pop()
        return result

    def prepare_turn(self, db, conversation, *, current_input="", current_message=None):
        return self.load(db, conversation, current_input=current_input, current_message=current_message)

    def load(self, db, conversation, *, current_input="", current_message=None, background=False):
        if not self.settings.memory_enabled:
            return super().load(db, conversation, current_input=current_input)
        current_input = redact_pii(current_input)
        summary = ensure_summary(db, conversation.id)
        db.commit()
        upper = snapshot_upper(db, conversation.id, current_message.id if current_message else None)
        diagnostics = {"calls": [], "budget_source": "utf8_upper_estimate", "errors": []}
        incomplete = False
        if upper == 0:
            previous = db.scalar(select(Conversation).where(
                    Conversation.student_id == conversation.student_id, Conversation.id != conversation.id,
                ).order_by(Conversation.updated_at.desc()).limit(1))
            if previous:
                try:
                    if background or self.settings.memory_foreground_model_calls:
                        diagnostics["calls"].extend(self.extract_pending(db, previous, background=background))
                    else:
                        previous_upper = snapshot_upper(db, previous.id)
                        enqueue_tool_job(db, job_type="MEMORY_EXTRACT",
                            idempotency_key=f"memory-switch:{previous.id}:{previous_upper}",
                            payload={"conversation_id": previous.id, "through": previous_upper}, priority=10)
                        db.commit()
                except Exception as exc:
                    db.rollback()
                    diagnostics["errors"].append(f"previous_session:{type(exc).__name__}")
                    if hasattr(exc, "memory_usage"):
                        diagnostics["calls"].append(exc.memory_usage)
                    incomplete = True
        summary = db.get(ConversationSummary, conversation.id, populate_existing=True)
        if current_message is not None and needs_reconcile(current_input):
            pending_correction(db, conversation, current_message)
            enqueue_tool_job(db, job_type="MEMORY_RECONCILE",
                             idempotency_key=f"memory-correct:{current_message.public_id}",
                             payload={"conversation_id": conversation.id, "message_id": current_message.id}, priority=20)
            db.commit()
            recent = read_window(db, conversation.id, 0, upper, 8, latest=True)
            related = related_records(db, conversation.student_id, self._items(summary, include_disputed=True), recent, current_input)
            if self.settings.memory_foreground_model_calls and (related or self._items(summary, include_disputed=True)):
                try:
                    diagnostics["calls"].append(self._run_update(db, conversation, summary, recent,
                                                                mode="correct", focus=current_input, current=current_message))
                except Exception as exc:
                    db.rollback()
                    incomplete = True
                    diagnostics["errors"].append(f"correction:{type(exc).__name__}")
                    if hasattr(exc, "memory_usage"):
                        diagnostics["calls"].append(exc.memory_usage)
                    summary = db.get(ConversationSummary, conversation.id, populate_existing=True)
                    enqueue_tool_job(db, job_type="MEMORY_RECONCILE",
                                     idempotency_key=f"memory-correct:{current_message.public_id}",
                                     payload={"conversation_id": conversation.id, "message_id": current_message.id}, priority=20)
                    db.commit()
        budget = self._history_budget(current_input)
        hit, total = False, 0
        for attempt in range(self.settings.memory_foreground_max_batches + 1):
            summary = db.get(ConversationSummary, conversation.id, populate_existing=True)
            messages, total, hit = self._load_window(db, conversation, summary.summarized_until_message_id, upper)
            brief = "\n".join(item["text"] for item in self._items(summary))
            cost = estimate_tokens(brief) + sum(estimate_tokens(item.content) + 8 for item in messages)
            if total <= len(messages) and cost <= budget:
                break
            if attempt == self.settings.memory_foreground_max_batches:
                incomplete = True
                break
            if not background and not self.settings.memory_foreground_model_calls:
                incomplete = True
                break
            keep = min(max(1, self.settings.memory_recent_messages), len(messages)) if total == len(messages) else 0
            batch = self._batch(messages, keep=keep)
            if not batch:
                incomplete = True
                diagnostics["errors"].append("uncompressible_recent_or_oversized_message")
                break
            try:
                diagnostics["calls"].append(self._run_update(db, conversation, summary, batch,
                                                            mode="compact", background=background))
            except Exception as exc:
                db.rollback()
                incomplete = True
                diagnostics["errors"].append(f"compact:{type(exc).__name__}")
                if hasattr(exc, "memory_usage"):
                    diagnostics["calls"].append(exc.memory_usage)
                break
        summary = db.get(ConversationSummary, conversation.id, populate_existing=True)
        items = self._items(summary)
        brief = "\n".join(item["text"] for item in items)
        if incomplete or total > len(messages):
            messages = read_window(db, conversation.id, summary.summarized_until_message_id, upper,
                                   self.settings.memory_page_size, latest=True)
        remaining = max(0, budget - estimate_tokens(brief))
        selected = []
        for item in reversed(messages):
            cost = estimate_tokens(item.content) + 8
            if cost > remaining:
                incomplete = True
                break
            selected.insert(0, item)
            remaining -= cost
        if estimate_tokens(brief) > budget:
            brief, incomplete = "", True
        history_incomplete = incomplete
        memories, retrieval = recall(db, self.settings, self.index, conversation.student_id,
                                     current_input, selected, items if brief else [])
        diagnostics.update(retrieval)
        pending = list(db.scalars(select(MemoryEvent).join(Message, Message.public_id == MemoryEvent.source_message_id)
            .where(MemoryEvent.student_id == conversation.student_id, MemoryEvent.relation == "reconcile",
                   MemoryEvent.status == "pending", Message.status.in_(("completed", "generating")))
            .order_by(MemoryEvent.id.desc()).limit(3)))
        diagnostics["pending_corrections"] = []
        warning_budget = self.settings.memory_correction_tokens
        for event in pending:
            text = f"{event.created_at.isoformat()} 原话：{event.quote}"
            cost = estimate_tokens(text) + 8
            if cost <= warning_budget:
                diagnostics["pending_corrections"].insert(0, text)
                warning_budget -= cost
        fallback = raw_history(db, self.settings, conversation.student_id, current_input, selected,
                               budget=self.settings.memory_raw_recall_tokens)
        preferences = stable_preferences(db, self.settings, conversation.student_id, selected)
        known = {m["memory_id"] for m in memories}
        memories = tuple(memories) + tuple(m for m in preferences + fallback if m["memory_id"] not in known)
        diagnostics["raw_history_count"] = len(fallback)
        incomplete |= bool(pending)
        incomplete |= retrieval["retrieval"] == "unavailable"
        if history_incomplete and upper > summary.summarized_until_message_id:
            enqueue_tool_job(db, job_type="MEMORY_COMPACT",
                             idempotency_key=f"memory-compact:{conversation.id}:{summary.version}:{upper}",
                             payload={"conversation_id": conversation.id, "through": upper}, priority=10)
            db.commit()
        diagnostics.update(history_budget=budget, summary_version=summary.version,
                           needs_clarification=any(item.get("needs_clarification") for item in diagnostics["calls"]),
                           boundary=summary.summarized_until_message_id,
                           selected_memory_ids=[item["memory_id"] for item in memories])
        return MemoryContext(conversation.public_id, brief, tuple(selected), "redis" if hit else "database",
                             hit, total, min(total, self.settings.short_term_memory_max_messages),
                             bool(summary.summarized_until_message_id), summary.version,
                             summary.summarized_until_message_id, memories, incomplete, diagnostics)

    def extract_pending(self, db, conversation, *, background=False, through=None):
        calls = []
        for _ in range(self.settings.memory_foreground_max_batches):
            summary = ensure_summary(db, conversation.id)
            upper = snapshot_upper(db, conversation.id)
            if through is not None:
                upper = min(upper, through)
            lower = summary.extracted_until_message_id
            messages = read_window(db, conversation.id, lower, upper, self.settings.memory_page_size)
            if not messages:
                db.commit()
                break
            batch = self._batch(messages, keep=0)
            if not batch:
                raise ValueError("extraction message exceeds batch budget")
            calls.append(self._run_update(db, conversation, summary, batch, mode="extract", background=background))
        return calls

    def enqueue_after_turn(self, db, conversation, user_message, assistant_message):
        if not self.settings.memory_enabled:
            return
        job = enqueue_tool_job(db, job_type="MEMORY_EXTRACT",
                               idempotency_key=f"memory-extract:{conversation.id}:{assistant_message.id}",
                               payload={"conversation_id": conversation.id, "through": assistant_message.id}, priority=10)
        job.run_after = utc_now() + timedelta(seconds=0 if needs_reconcile(user_message.redacted_content or "")
                                             else self.settings.memory_idle_seconds)
        if needs_reconcile(user_message.redacted_content or user_message.content):
            pending_correction(db, conversation, user_message)
            enqueue_tool_job(db, job_type="MEMORY_RECONCILE",
                idempotency_key=f"memory-correct:{user_message.public_id}",
                payload={"conversation_id": conversation.id, "message_id": user_message.id}, priority=20)
        # Start processing before the foreground history overflows.
        state = ensure_summary(db, conversation.id)
        tail = read_window(db, conversation.id, state.summarized_until_message_id, assistant_message.id,
                           self.settings.memory_page_size)
        if sum(estimate_tokens(m.content) + 8 for m in tail) >= self.settings.memory_history_tokens * 0.75:
            job.run_after = utc_now()
            enqueue_tool_job(db, job_type="MEMORY_COMPACT",
                idempotency_key=f"memory-compact:{conversation.id}:{state.version}:{assistant_message.id}",
                payload={"conversation_id": conversation.id, "through": assistant_message.id}, priority=10)

    def execute_job(self, db, job):
        payload = json.loads(job.payload_json)
        if job.job_type == "MEMORY_CALL":
            return {"status": "interrupted", "usage_source": "unknown"}
        if job.job_type == "MEMORY_INDEX_SYNC":
            row = db.scalar(select(UserMemory).where(UserMemory.public_id == payload["memory_id"],
                                                     UserMemory.student_id == payload["student_id"]))
            if row is None:
                return {"status": "missing"}
            version = row.version
            db.commit()
            if row.status == "active":
                self.index.upsert(memory_id=row.public_id, student_id=row.student_id, content=row.content, version=version)
            else:
                self.index.delete(row.public_id)
            count = db.execute(update(UserMemory).where(UserMemory.id == row.id, UserMemory.version == version)
                               .values(indexed_version=version, index_signature=self.index.signature))
            if count.rowcount != 1:
                db.rollback()
                raise MemoryConflict("memory changed during index sync")
            db.commit()
            return {"status": "indexed", "version": version}
        conversation = db.get(Conversation, payload["conversation_id"])
        if conversation is None:
            return {"status": "missing"}
        if job.job_type == "MEMORY_RECONCILE":
            current = db.get(Message, payload["message_id"])
            if current is None or current.conversation_id != conversation.id:
                return {"status": "missing_source"}
            summary = ensure_summary(db, conversation.id)
            if current.status == "generating":
                raise MemoryConflict("correction source is not committed yet")
            if current.status != "completed":
                return {"status": "failed_source"}
            event = pending_correction(db, conversation, current)
            if event.status == "committed":
                return {"status": "already_committed"}
            db.commit()
            # A correction may have arrived before its old fact was extracted.
            self.extract_pending(db, conversation, background=True, through=current.id - 1)
            summary = ensure_summary(db, conversation.id)
            remaining = db.scalar(window_query(conversation.id, summary.extracted_until_message_id,
                                              current.id - 1).with_only_columns(Message.id).limit(1))
            if remaining:
                raise MemoryConflict("extract correction prerequisites before retry")
            recent = read_window(db, conversation.id, 0, current.id-1, 8, latest=True)
            usage = self._run_update(db, conversation, summary, recent, mode="correct",
                                     focus=current.redacted_content or current.content, current=current, background=True)
            if usage.get("unresolved_targets"):
                raise MemoryConflict("some correction targets remain unverified; retry with committed versions")
            return {"calls": [usage]}
        if job.job_type == "MEMORY_EXTRACT":
            if db.scalar(select(Message.id).where(Message.conversation_id == conversation.id,
                                                 Message.status == "generating").limit(1)):
                raise MemoryConflict("conversation has an active turn")
            if snapshot_upper(db, conversation.id) != payload["through"]:
                return {"status": "superseded"}
            calls = self.extract_pending(db, conversation, background=True)
            summary = db.get(ConversationSummary, conversation.id, populate_existing=True)
            if summary.extracted_until_message_id < payload["through"]:
                raise MemoryConflict("extraction backlog remains; retry next batch")
            return {"calls": calls}
        if job.job_type == "MEMORY_COMPACT":
            if db.scalar(select(Message.id).where(Message.conversation_id == conversation.id,
                                                 Message.status == "generating").limit(1)):
                raise MemoryConflict("conversation has an active turn")
            context = self.load(db, conversation, background=True)
            if any(error.startswith("compact:") for error in context.diagnostics["errors"]):
                raise MemoryConflict("compaction incomplete; retry from committed boundary")
            return context.diagnostics
        raise ValueError("unknown memory job")


ShortTermMemoryService = MemoryService
