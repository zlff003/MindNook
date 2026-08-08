from __future__ import annotations

import re
from dataclasses import dataclass


CONTRAST_WORDS = ("但", "但是", "不过", "可是", "然而", "却")

CLEAR_DENIAL_PATTERNS = (
    re.compile(r"我(?:现在)?(?:不想|不会|没有想过|没想过)自杀"),
    re.compile(r"我(?:现在)?(?:不会|没有)(?:想过)?伤害自己"),
)

# 固定规则只做三件事：明确危险表达建立不可降级的高风险下限；
# 明确否认只说明当前未发现明确意图，不能抵消同一消息里的其他危险表达；
# 其余风险相关内容至少进入心理支持和 SafetyAgent 评估，不能直接归为普通问答。
CLEAR_DANGER_PATTERNS = (
    re.compile(r"我(?:现在|已经)?(?:就)?(?:想|要|准备|打算)(?:去)?(?:自杀|死|结束自己的生命)"),
    re.compile(r"我(?:现在)?有(?:自杀|自伤|伤害自己|伤害别人)的?(?:想法|念头)"),
    re.compile(
        r"我(?:现在|已经)?(?:想|要|准备|打算|控制不住想).*?(?:伤害自己|自残)"
    ),
    re.compile(
        r"我(?:现在|已经)?(?:想|要|准备|打算|控制不住想).*?(?:杀人|杀了?[他她]|伤害别人)"
    ),
    re.compile(r"我(?:现在|已经)?(?:想|要|准备|打算).*?跳下去"),
)

CLEAR_DANGER_PHRASES = (
    "不想活了",
    "不想再活",
    "活不下去了",
    "现在就去死",
)

RISK_TOPIC_KEYWORDS = (
    "自杀",
    "自残",
    "伤害自己",
    "杀人",
    "伤害别人",
    "不想活",
    "去死",
    "结束生命",
    "结束自己的生命",
    "跳下去",
    "死亡",
)

SUPPORT_KEYWORDS = (
    "压力",
    "焦虑",
    "紧张",
    "低落",
    "难过",
    "失眠",
    "睡不着",
    "孤独",
    "崩溃",
    "恐慌",
    "情绪",
    "关系",
    "适应",
    "内耗",
    "无助",
    "绝望",
)

MEDIUM_RISK_KEYWORDS = (
    "每天",
    "持续",
    "很久",
    "严重影响",
    "无法上课",
    "无法学习",
    "彻夜",
    "完全撑不住",
)

FORBIDDEN_OUTPUT_MARKERS = (
    "你的风险等级",
    "风险评分",
    "模型置信度",
    "后台报告",
    "诊断为",
    "psychological_support",
    "safety_risk",
    "risk_level",
    "内部风险",
)


@dataclass(frozen=True)
class Assessment:
    dialogue_type: str
    risk_level: str | None
    confidence: float
    emotion_summary: str
    reason_summary: str
    hard_rule_hit: bool = False


def _is_clear_denial(text: str) -> bool:
    return not any(word in text for word in CONTRAST_WORDS) and any(
        pattern.search(text) for pattern in CLEAR_DENIAL_PATTERNS
    )


def _has_clear_danger(text: str) -> bool:
    return any(pattern.search(text) for pattern in CLEAR_DANGER_PATTERNS) or any(
        phrase in text for phrase in CLEAR_DANGER_PHRASES
    )


def assess_message(text: str) -> Assessment:
    normalized = "".join(text.lower().split())

    # 明确危险证据独立判断，消息其他位置的否认不能遮蔽自伤或伤人表达。
    if _has_clear_danger(normalized):
        return Assessment(
            dialogue_type="safety_risk",
            risk_level="high",
            confidence=0.99,
            emotion_summary="学生表达了与当前人身安全相关的严重痛苦。",
            reason_summary="固定安全规则识别到明确的伤害意图或即时危险表达。",
            hard_rule_hit=True,
        )

    support_hits = [keyword for keyword in SUPPORT_KEYWORDS if keyword in normalized]
    if support_hits:
        medium = any(keyword in normalized for keyword in MEDIUM_RISK_KEYWORDS)
        return Assessment(
            dialogue_type="psychological_support",
            risk_level="medium" if medium else "low",
            confidence=0.86 if medium else 0.78,
            emotion_summary=f"学生表达了与{support_hits[0]}相关的困扰。",
            reason_summary=(
                "困扰持续或已明显影响日常生活，暂未发现明确即时伤害意图。"
                if medium
                else "存在情绪或适应困扰，暂未发现明确即时危险信号。"
            ),
        )

    risk_topic_hits = [
        keyword for keyword in RISK_TOPIC_KEYWORDS if keyword in normalized
    ]
    if risk_topic_hits:
        clear_denial = _is_clear_denial(normalized)
        return Assessment(
            dialogue_type="psychological_support",
            risk_level="low",
            confidence=0.72 if clear_denial else 0.62,
            emotion_summary="学生提到了与人身安全相关的内容，需要谨慎理解。",
            reason_summary=(
                "学生明确否认了当前伤害意图，固定规则未发现其他明确危险表达。"
                if clear_denial
                else "存在风险相关表达，但固定规则无法确认当前明确意图，需要进一步评估。"
            ),
        )

    return Assessment(
        dialogue_type="general",
        risk_level=None,
        confidence=0.9,
        emotion_summary="",
        reason_summary="内容属于普通学习、校园生活或日常问答。",
    )


def review_student_response(response: str) -> bool:
    return not any(marker in response for marker in FORBIDDEN_OUTPUT_MARKERS)


def high_risk_fallback() -> str:
    return (
        "听起来你现在正承受非常强烈的痛苦，谢谢你把这件事告诉我。"
        "此刻先不要独处，请马上去一个有人在场的安全地方，并联系一位你信任的人，"
        "请对方现在陪着你。接下来的 10 分钟里，先把可能让你受伤的物品放远，"
        "然后联系学校的正式支持渠道、校园安全部门或当地紧急服务。"
        "你现在身边有没有可以马上联系并陪着你的人？"
    )
