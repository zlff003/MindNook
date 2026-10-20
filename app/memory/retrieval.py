from __future__ import annotations

import json

from sqlalchemy import or_, select

from app.memory.repository import memory_dict
from app.models import UserMemory
from app.services.context_budget import estimate_tokens


def retrieval_query(current_input, recent) -> str:
    # Keep both roles to resolve references such as “那个办法”. Current input leads.
    context = "\n".join(f"{item.model_role}: {item.content[:180]}" for item in recent[-4:])
    return f"当前：{current_input}\n近期：{context}"


def recall(db, settings, index, student_id, current_input, recent, summary_items):
    query = retrieval_query(current_input, recent)
    covered = {item.message_id for item in recent if not item.truncated}
    summary_text = "".join("".join(item["text"].split()) for item in summary_items)
    diagnostics = {"retrieval": "ok", "discarded": [], "candidate_count": 0}
    # No vector initialization/download for a new student without any memories.
    if not db.scalar(select(UserMemory.id).where(UserMemory.student_id == student_id,
                                                 UserMemory.status == "active").limit(1)):
        return (), diagnostics
    try:
        hits = index.search(student_id, query, settings.memory_candidate_k)
        # SQL overlay makes newly committed short-session memories available before
        # their outbox job runs. Current bodies are scored, never stale vector text.
        seen_ids = {item[0] for item in hits}
        pending = list(db.scalars(select(UserMemory).where(UserMemory.student_id == student_id,
            UserMemory.status == "active", or_(UserMemory.indexed_version < UserMemory.version,
                                               UserMemory.index_signature != index.signature))
            .order_by(UserMemory.updated_at.desc()).limit(settings.memory_candidate_k)))
        for row in pending:
            if row.public_id not in seen_ids:
                hits.append((row.public_id, index.similarity(query, row.content), row.version))
        diagnostics["candidate_count"] = len(hits)
        candidates = []
        for memory_id, score, indexed_version in hits:
            row = db.scalar(select(UserMemory).where(UserMemory.public_id == memory_id,
                                                      UserMemory.student_id == student_id,
                                                      UserMemory.status == "active"))
            reason = None
            if row is None:
                reason = "inactive_or_wrong_owner"
            else:
                if indexed_version != row.version:
                    score = index.similarity(query, row.content)
                evidence = [item for item in json.loads(row.evidence_json) if not item.get("invalidated")]
                sources = {item["message_id"] for item in evidence}
                if score < settings.memory_min_similarity:
                    reason = "irrelevant"
                # Summary coverage is evidence-based, never just a shared topic.
                elif sources and (sources <= covered or "".join(row.content.split()) in summary_text):
                    reason = "covered"
            if reason:
                diagnostics["discarded"].append({"memory_id": memory_id, "reason": reason})
            else:
                candidates.append((score, row.updated_at, row))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected, seen = [], set()
        remaining = settings.memory_recall_tokens
        for score, _, row in candidates:
            key = "".join(row.content.split())
            cost = estimate_tokens(memory_dict(row)["context_content"]) + 100
            if key in seen or cost > remaining:
                continue
            selected.append(dict(memory_dict(row), relevance=round(score, 4)))
            seen.add(key)
            remaining -= cost
            if len(selected) == settings.memory_top_k:
                break
        return tuple(selected), diagnostics
    except Exception as exc:  # optional retrieval must not block a conversation
        diagnostics.update(retrieval="unavailable", error=type(exc).__name__)
        return (), diagnostics
