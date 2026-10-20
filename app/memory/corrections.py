from __future__ import annotations

import json
import re

from sqlalchemy import select

from app.memory.repository import source_ids
from app.models import UserMemory


CORRECTION_MARKERS = ("说错", "更正", "纠正", "其实没", "并没有", "不是这样", "记错", "没有试过", "没试过")
FEEDBACK_MARKERS = ("没用", "没帮助", "有帮助", "有效", "无效", "试过", "尝试了", "希望你以后", "每次只", "一直希望")


def is_correction(text):
    return any(marker in text for marker in CORRECTION_MARKERS)


def needs_reconcile(text):
    return is_correction(text) or any(marker in text for marker in FEEDBACK_MARKERS)


def related_records(db, student_id, summary_items, recent, text, *, limit=12, before_id=None):
    """Bounded evidence/lexical candidates. Ambiguous pronouns are left to the model."""
    rows = list(db.scalars(select(UserMemory).where(UserMemory.student_id == student_id,
                                                    UserMemory.status.in_(("active", "disputed")))
                           .order_by(UserMemory.updated_at.desc()).limit(200)))
    if before_id is not None:
        rows = [r for r in rows if all(e.get("db_id", 0) < before_id for e in json.loads(r.evidence_json)
                                      if not e.get("invalidated"))]
    sources = {item.message_id for item in recent}
    sources.update(source for item in summary_items for source in item["source_message_ids"])
    query = text + " ".join(item.content for item in recent[-4:])
    phrases = {query[i:i+2] for i in range(len(query)-1) if re.match(r"[\u3400-\u9fff]{2}", query[i:i+2])}
    ranked = sorted(rows, key=lambda row: (bool(source_ids(row) & sources),
                    sum(phrase in row.content for phrase in phrases), row.updated_at), reverse=True)
    return [row for row in ranked if source_ids(row) & sources or any(phrase in row.content for phrase in phrases)][:limit]

