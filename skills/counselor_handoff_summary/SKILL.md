---
name: counselor_handoff_summary
description: 为所有 CONSULT 与 RISK 报告生成事实清楚、便于辅导员或管理员接手且不面向学生的后台交接摘要。
---

# Counselor Handoff Summary

## Workflow

1. 仅生成工作人员可见的事实摘要，不使用学生端安慰口吻，也不作临床诊断。
2. 按固定顺序呈现报告 ID、学生身份、风险等级、情绪标签、置信度、模型摘要、建议跟进动作和学生原始表达摘录。
3. 原始表达只做必要长度的摘录，不添加学生未表达的事实。
4. CONSULT 建议工作人员结合实际需要复核困扰与支持需求；RISK 建议优先核实当前安全、陪伴与现实支持连接情况。
5. 使用以下单段模板渲染：

   `报告ID：{report_id}；学生：{student_identity}；风险等级：{risk_level}；情绪标签：{emotion_summary}；置信度：{confidence}；模型摘要：{reason_summary}；建议跟进：{follow_up_action}；原始表达摘录：{student_expression_excerpt}`
