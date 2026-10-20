"""Owner-filtered original-message fallback, bundled with later correction evidence."""
import json
import re

from sqlalchemy import or_, select

from app.models import Conversation, MemoryEvent, Message, UserMemory
from app.services.context_budget import estimate_tokens
from app.services.privacy import redact_pii


def query_terms(text):
    aliases = (("睡", "入睡", "凌晨", "睡眠"), ("清单", "待办", "列待办"),
               ("散步", "走路"), ("原因", "因为", "担心"), ("几点", "凌晨", "点", "入睡"))
    terms = set(re.findall(r"[a-zA-Z0-9]{2,}", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i:i+2] for i in range(len(run)-1))
    for group in aliases:
        if any(w in text for w in group):
            terms.update(group)
    terms -= {"之前", "什么", "我的", "现在", "这个", "那个", "记得", "不知道", "一开始",
              "我们", "当时", "一般", "时间", "原因", "最早", "最初", "的是", "有什么"}
    return sorted(terms, key=lambda t: (-len(t), t))[:30]


def raw_history(db, settings, student_id, query, recent, *, budget=None):
    terms = query_terms(query)
    budget = settings.memory_raw_recall_tokens if budget is None else budget
    if not terms or budget <= 0:
        return []
    base = select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
        Conversation.student_id == student_id, Message.sender_type == "student", Message.status == "completed",
        or_(*(Message.redacted_content.contains(t, autoescape=True) for t in terms),
            *(Message.content.contains(t, autoescape=True) for t in terms)))
    limit = max(1, settings.memory_raw_candidate_limit // 2)
    # Both ends are searched so early facts are not evicted by hundreds of recent matches.
    rows = {r.id: r for r in db.scalars(base.order_by(Message.id.asc()).limit(limit))}
    rows.update({r.id: r for r in db.scalars(base.order_by(Message.id.desc()).limit(limit))})
    covered = {m.message_id for m in recent if not m.truncated}
    events = list(db.execute(select(MemoryEvent, Message.id).join(Message, Message.public_id == MemoryEvent.source_message_id)
        .where(MemoryEvent.student_id == student_id, Message.status == "completed",
               MemoryEvent.relation.in_(("correction", "evolution", "reconcile")))
        .order_by(Message.id.asc())))
    from app.memory.corrections import CORRECTION_MARKERS
    raw_corrections = list(db.scalars(select(Message).join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.student_id == student_id, Message.status == "completed", Message.sender_type == "student",
               or_(*(Message.content.contains(marker, autoescape=True) for marker in CORRECTION_MARKERS)))
        .order_by(Message.id.asc())))
    earliest = any(w in query for w in ("最初", "最早", "一开始", "原来"))
    ranked = sorted(rows.values(), key=lambda r: (-r.id if earliest else
                    sum(t in (r.redacted_content or r.content) for t in terms), r.id), reverse=True)
    result = []
    for row in ranked:
        if row.public_id in covered:
            continue
        quote = redact_pii(row.redacted_content or row.content)
        # Include every potentially relevant later correction as a single bundle.
        # If it cannot fit, omit the old claim rather than expose it without its correction.
        corrections = []
        for later in raw_corrections:
            text = redact_pii(later.redacted_content or later.content)
            if later.id > row.id and set(query_terms(quote)) & set(query_terms(text)):
                corrections.append(text)
        for event, source_id in events:
            if source_id <= row.id:
                continue
            before = json.loads(event.before_json)
            targets_source = any(e.get("message_id") == row.public_id for e in before.get("evidence", []))
            if targets_source or (event.relation == "reconcile" and event.status == "pending"
                                  and set(query_terms(quote)) & set(query_terms(event.quote))):
                if event.quote not in corrections:
                    corrections.append(event.quote)
        content = "历史原话（只代表当时陈述，须结合后续更正）：" + quote
        if corrections:
            content += "\n后续核对/更正原话：" + "\n".join(corrections)
        cost = estimate_tokens(content) + 100
        if cost > budget:
            continue
        result.append({"memory_id": "message:" + row.public_id, "content": content, "type": "raw_history",
                       "updated_at": row.created_at.isoformat(), "evidence": [{"message_id": row.public_id,
                       "conversation_id": row.conversation_id, "quote": quote}], "relevance": None})
        budget -= cost
        if len(result) == 3:
            break
    return result


def stable_preferences(db, settings, student_id, recent):
    from app.memory.types import is_stable_preference
    covered = {m.message_id for m in recent}
    rows = list(db.scalars(select(UserMemory).where(UserMemory.student_id == student_id,
                     UserMemory.status == "active", UserMemory.type == "preference")
                     .order_by(UserMemory.updated_at.desc()).limit(200)))
    rows = [r for r in rows if is_stable_preference(r.content)]
    rows.sort(key=lambda r: (sum(w in r.content for w in ("每次", "一直", "长期", "回复", "简短")),
                             r.updated_at), reverse=True)
    from app.memory.repository import memory_dict
    result, budget = [], settings.memory_preference_tokens
    for row in rows:
        evidence = [e for e in json.loads(row.evidence_json) if not e.get("invalidated")]
        if not evidence or all(e["message_id"] in covered for e in evidence):
            continue
        cost = estimate_tokens(memory_dict(row)["context_content"]) + 100
        if cost <= budget:
            result.append(dict(memory_dict(row), relevance=None))
            budget -= cost
    return result
