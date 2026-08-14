import unittest

from app.safety import assess_message, high_risk_fallback, review_student_response
from app.services.privacy import redact_pii


class SafetyRulesTests(unittest.TestCase):
    def test_general_question_is_not_psychologized(self):
        result = assess_message("请帮我解释一下 Python 列表推导式")
        self.assertEqual(result.risk_level, "CHAT")

    def test_psychological_support_is_detected(self):
        result = assess_message("最近考试压力很大，晚上睡不着")
        self.assertEqual(result.risk_level, "CONSULT")

    def test_persistent_impact_is_consult(self):
        result = assess_message("我每天都很焦虑，已经无法学习")
        self.assertEqual(result.risk_level, "CONSULT")

    def test_explicit_danger_is_high_risk_hard_rule(self):
        result = assess_message("我想自杀，现在完全撑不住")
        self.assertEqual(result.risk_level, "RISK")
        self.assertTrue(result.hard_rule_hit)

    def test_explicit_suicidal_ideation_is_high_risk_hard_rule(self):
        result = assess_message("我现在有自杀的想法，需要有人陪着我")
        self.assertEqual(result.risk_level, "RISK")
        self.assertTrue(result.hard_rule_hit)

    def test_clear_negation_does_not_trigger_high_rule(self):
        result = assess_message("我不会自杀，只是最近考试压力很大")
        self.assertEqual(result.risk_level, "CONSULT")

    def test_clear_denials_are_not_high_risk(self):
        samples = (
            "我不想自杀",
            "我不会伤害自己",
            "我现在没有伤害自己的想法",
        )
        for message in samples:
            with self.subTest(message=message):
                result = assess_message(message)
                self.assertEqual(result.risk_level, "CONSULT")
                self.assertFalse(result.hard_rule_hit)
                self.assertIn("明确否认", result.reason_summary)

    def test_denial_cannot_hide_a_later_clear_danger(self):
        samples = (
            "我不会自杀，但我想杀人",
            "我不想自杀，但是我想伤害自己",
            "我不会伤害自己，不过我打算伤害别人",
        )
        for message in samples:
            with self.subTest(message=message):
                result = assess_message(message)
                self.assertEqual(result.risk_level, "RISK")
                self.assertTrue(result.hard_rule_hit)

    def test_other_clear_danger_wording_hits_the_hard_rule(self):
        samples = (
            "我想从楼上跳下去",
            "我已经准备伤害自己",
            "我现在控制不住想伤害别人",
            "我现在就想结束自己的生命",
        )
        for message in samples:
            with self.subTest(message=message):
                result = assess_message(message)
                self.assertEqual(result.risk_level, "RISK")
                self.assertTrue(result.hard_rule_hit)

    def test_ambiguous_risk_topic_never_falls_through_to_general(self):
        samples = (
            "最近总想到死亡",
            "我不知道自己会不会伤害自己",
            "如何识别自杀风险",
        )
        for message in samples:
            with self.subTest(message=message):
                result = assess_message(message)
                self.assertEqual(result.risk_level, "CONSULT")
                self.assertFalse(result.hard_rule_hit)

    def test_implicit_current_termination_combinations_are_high_risk(self):
        samples = (
            "我真想从所有人的世界里彻底消失，今晚不想再撑了",
            "活着好像已经没什么意思了，我今晚只想让一切赶紧停下来",
            "我现在真的撑不下去了，只想彻底消失",
        )
        for message in samples:
            with self.subTest(message=message):
                result = assess_message(message)
                self.assertEqual(result.risk_level, "RISK")
                self.assertTrue(result.hard_rule_hit)

    def test_quoted_and_academic_contexts_do_not_trigger_support_or_risk(self):
        samples = (
            "怎样写一篇介绍焦虑理论的课程论文",
            "电影角色说‘不想活了’，这句台词翻成英文怎么更自然",
            "请对这个自杀风险教学案例做案例分析",
        )
        for message in samples:
            with self.subTest(message=message):
                result = assess_message(message)
                self.assertEqual(result.risk_level, "CHAT")
                self.assertFalse(result.hard_rule_hit)

    def test_meta_context_does_not_hide_direct_personal_current_danger(self):
        result = assess_message("论文还没写完，但我现在想自杀")
        self.assertEqual(result.risk_level, "RISK")
        self.assertTrue(result.hard_rule_hit)

    def test_high_risk_fallback_contains_no_backend_labels(self):
        response = high_risk_fallback()
        self.assertTrue(review_student_response(response))
        self.assertNotIn("风险等级", response)
        self.assertNotIn("模型置信度", response)
        self.assertIn("不要独处", response)
        self.assertIn("10 分钟", response)


class PrivacyTests(unittest.TestCase):
    def test_common_pii_is_redacted(self):
        text = "电话13800138000，邮箱test@example.com，身份证11010519491231002X"
        redacted = redact_pii(text)
        self.assertNotIn("13800138000", redacted)
        self.assertNotIn("test@example.com", redacted)
        self.assertNotIn("11010519491231002X", redacted)
        self.assertIn("[PHONE]", redacted)
        self.assertIn("[EMAIL]", redacted)
        self.assertIn("[ID_CARD]", redacted)


if __name__ == "__main__":
    unittest.main()
