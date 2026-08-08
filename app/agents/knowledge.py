from __future__ import annotations


BUILTIN_SUPPORT_KNOWLEDGE = (
    {
        "keywords": ("压力", "考试", "作业", "学习", "焦虑"),
        "text": "压力支持：先确认当前最紧迫的压力源，再把任务缩小为十分钟内可执行的一步；建议数量保持一到两个。",
    },
    {
        "keywords": ("失眠", "睡不着", "睡眠"),
        "text": "睡眠支持：避免承诺立即入睡，可建议短暂离开床铺、降低刺激、记录担忧，并在持续影响生活时寻求专业支持。",
    },
    {
        "keywords": ("人际", "室友", "同学", "家人", "关系"),
        "text": "关系支持：先澄清具体事件和感受，可建议使用事实—感受—需要的表达方式，并寻找可信任的现实支持。",
    },
    {
        "keywords": ("低落", "难过", "孤独", "无助", "情绪"),
        "text": "低落支持：回应具体处境，不贴标签；鼓励一项低门槛行动和一次现实连接，持续或加重时建议联系正式支持。",
    },
)


def retrieve_builtin_knowledge(message: str, topics: list[str], limit: int = 3) -> list[str]:
    haystack = f"{message} {' '.join(topics)}".lower()
    ranked: list[tuple[int, str]] = []
    for item in BUILTIN_SUPPORT_KNOWLEDGE:
        score = sum(1 for keyword in item["keywords"] if keyword.lower() in haystack)
        if score:
            ranked.append((score, item["text"]))
    ranked.sort(key=lambda value: value[0], reverse=True)
    return [text for _, text in ranked[:limit]]

