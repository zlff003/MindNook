"""Deterministic offline contract fixtures; never evidence of real semantic quality."""
from app.memory.corrections import is_correction


def mock_memory_update(payload):
    mode = payload["mode"]
    if mode == "verify":
        return {"decisions": [{"memory_id": p["operation"]["memory_id"],
            "approved": not ("散步" in p["old_fact"]["content"] and
                             any(s in payload["current_message"]["content"] for s in ("散步是我的", "散步是我自己")))}
            for p in payload["proposals"]]}
    if mode == "correct":
        current, related = payload["current_message"], payload["related_memories"]
        text = current["content"]
        targets = [r for r in related if any(t in text for t in r["topics"])]
        # A repeated quotation is confirmation, not a fresh change.
        targets = [r for r in targets if r["content"] != text]
        if len(targets) != 1:
            return {"corrections": [], "needs_clarification": bool(related) and
                    any(t in text for t in ("那个", "这个办法"))}
        row = targets[0]
        correction = is_correction(text)
        return {"corrections": [{"memory_id": row["memory_id"], "expected_version": row["version"],
            "change": "correction" if correction else "evolution"}], "needs_clarification": False}
    facts = []
    keywords = ("考试", "睡眠", "失眠", "睡不", "焦虑", "压力", "室友", "列待办", "偏好",
                "每次只", "希望你以后", "散步", "凌晨", "宿舍")
    for msg in payload["messages"]:
        text = msg["content"]
        if msg["role"] != "student" or not any(w in text for w in keywords):
            continue
        preference = any(w in text for w in ("每次只", "希望你以后")) and "今天" not in text
        facts.append({"message_id": msg["message_id"], "quote": text,
            "type": "preference" if preference else "concern", "subject": "user",
            "stage": "reported", "topics": [w for w in keywords if w in text][:8]})
    return {"facts": facts, "reviewed_message_ids": [m["message_id"] for m in payload["messages"]]}
