# 心理场景 Skill 系统

> 文档版本：1.1
> 更新日期：2026-08-24
> 主要实现：`skills/*/SKILL.md`、`app/skills/registry.py`、`app/agents/runtime.py`、`app/services/reporting.py`

## 1. 定位与边界

MindNook 将心理场景中的表达规则从分散 Prompt 中拆出，沉淀为 `skills/*/SKILL.md`。每个 Skill 都是可扫描、可校验、可测试、可在管理端观察的项目资产。

Skill 只回答两类问题：

- 在特定心理场景中应该怎样表达；
- 哪些内容或表达方式不能出现。

Skill 不负责任务调度、风险定级、记忆读取、RAG 检索、数据库访问或工具调用。上述职责仍由 Agent Runtime、AgentHarness 和工具队列承担。

## 2. 与 Agent Runtime 的关系

```mermaid
flowchart LR
    U["学生输入"] --> UA["UnderstandingAgent<br/>理解意图"]
    UA --> SA1["SafetyAgent<br/>风险评估"]
    SA1 --> CA["ContextAgent<br/>记忆、RAG、Skill"]
    CA --> RA["ResponseAgent<br/>生成候选回复"]
    RA --> SA2["SafetyAgent<br/>按 Workflow 审查"]
    SA2 --> CO["CoordinatorAgent<br/>预算、任务板、最终采纳"]
```

各 Agent 的职责如下：

| 组件 | 与 Skill 的关系 |
|---|---|
| `UnderstandingAgent` | 判断意图和主题，为后续场景路由提供信息，不直接加载 Skill |
| `SafetyAgent` | 评估风险，并使用本轮已加载的 Workflow 审查候选回复；修复由 ResponseAgent 完成 |
| `ContextAgent` | 读取记忆、执行需要的 RAG 检索，并按风险与主题选择 Skill，将 Workflow 注入上下文 |
| `ResponseAgent` | 使用 Skill 上下文生成候选回复，不自行补写未加载 Skill 的规则或资源事实 |
| `CoordinatorAgent` | 管理任务板、模型调用预算、超时与最终采纳，不把调度逻辑交给 Skill |
| `AgentHarness` | 在 `CONSULT`、`RISK` 场景创建报告，并将后台交接摘要持久化 |

心理场景的具体话术原则以 Skill 为准；基础 Prompt 只保留 Agent 角色、输入输出协议和不可绕过的通用安全约束。

## 3. 目录和文件规范

所有 Skill 使用一目录一文件结构：

```text
skills/
├─ supportive_response_baseline/SKILL.md
├─ referral_resource_guidance/SKILL.md
├─ anxiety_grounding_support/SKILL.md
├─ sleep_routine_support/SKILL.md
├─ academic_stress_planning/SKILL.md
├─ high_risk_safety_plan/SKILL.md
└─ counselor_handoff_summary/SKILL.md
```

项目明确采用小写下划线命名，目录名必须与 frontmatter 中的 `name` 完全一致。每个 `SKILL.md` 的最小结构为：

```markdown
---
name: example_skill
description: 说明适用场景和主要作用。
---

# Example Skill

## Workflow

1. 说明模型应该怎样做。
2. 说明模型不能怎样做。
```

frontmatter 当前只允许 `name` 和 `description`。`Workflow` 使用大小写固定的二级标题 `## Workflow`，运行时只把该标题之后的内容作为场景规则载入。

## 4. 七个内置 Skill

| Skill | 适用场景 | 核心作用 | 面向对象 |
|---|---|---|---|
| `supportive_response_baseline` | 所有 `CONSULT` 回复 | 先回应具体感受；温和、具体、不评判；不诊断、不给药物建议、不泄露后台等级；一次给一到两个小步骤 | 学生 |
| `referral_resource_guidance` | `CONSULT`、`RISK` 的现实求助 | 把求助表达为可行下一步；可指向校园或当地支持资源；禁止编造电话、时间、人员和政策 | 学生 |
| `anxiety_grounding_support` | 焦虑、惊恐、崩溃、呼吸紧张、思绪停不下来 | 先承认难受，再给一个简单稳定动作；不作疾病诊断；出现明确危险时让位于高风险安全计划 | 学生 |
| `sleep_routine_support` | 失眠、睡不着、熬夜、作息紊乱 | 提供今晚能做的一到两个非药物小安排；不责备、不承诺效果；持续或影响功能时建议联系支持资源 | 学生 |
| `academic_stress_planning` | 考试、挂科、绩点、论文、作业和学业压力 | 将大压力拆成一个任务、一个时间块和一个停止点；不把成绩与人的价值绑定 | 学生 |
| `high_risk_safety_plan` | 所有 `RISK` 场景 | 停止一般情绪分析，使用短句提高未来十到十五分钟的安全性，连接现实支持，避免独处和危险条件 | 学生 |
| `counselor_handoff_summary` | 所有 `CONSULT`、`RISK` 报告 | 以固定字段生成事实化交接摘要，严格区分学生端语气和工作人员语气 | 辅导员、管理员 |

## 5. 路由规则

Skill 路由由 `SkillRegistry.select_response_skills()` 确定，风险字段统一为 `risk_level = CHAT / CONSULT / RISK`。

| 风险等级 | 学生回复 Skill | 心理 RAG | 报告与后台动作 |
|---|---|---|---|
| `CHAT` | 不加载心理 Skill | 不触发 | 不生成心理报告，不调用台账或风险工具 |
| `CONSULT` | 固定加载 `supportive_response_baseline`、`referral_resource_guidance`；按主题叠加焦虑、睡眠、学业 Skill | 触发 | 生成并保存心理报告、交接摘要和台账 |
| `RISK` | 加载 `referral_resource_guidance`、`high_risk_safety_plan` | 触发 | 生成并保存报告、交接摘要和台账；创建风险个案，个案成功后触发预警 |

`CONSULT` 的主题 Skill 通过学生消息和已识别主题中的确定性标记叠加：

- 焦虑类标记加载 `anxiety_grounding_support`；
- 睡眠类标记加载 `sleep_routine_support`；
- 学业类标记加载 `academic_stress_planning`；
- 同一轮可以同时加载多个主题 Skill。

`RISK` 优先执行高风险安全计划，不继续使用一般焦虑、睡眠或学业建议分散当前安全重点。

## 6. 扫描、校验和状态

`SkillRegistry` 扫描配置项 `skills_dir` 指向的目录，默认值为 `skills`。七个内置 Skill 属于必需资产：即使对应目录或文件被删除，扫描结果仍会展示该项并标记失败。

基础校验包括：

- 目录名只能包含小写字母、数字和下划线；
- 目录内必须存在 `SKILL.md`；
- 文件必须使用 UTF-8，并包含闭合的 YAML frontmatter；
- frontmatter 必须包含 `name` 和 `description`；
- `name` 必须与目录名一致；
- 正文必须包含 `## Workflow`；
- 空 `description`、空 Workflow 或未知 frontmatter 字段会产生警告。

状态含义如下：

| 状态 | 含义 | 是否注入 Runtime |
|---|---|---|
| `READY` | 结构与内容通过校验 | 是 |
| `WARN` | 可加载，但存在非阻断问题 | 是 |
| `FAILED` | 缺少必需结构、命名错误或无法读取 | 否 |

每个状态同时暴露 `issues`，便于管理员定位具体问题。管理端可在“系统运行 → Skills”查看，也可通过管理员接口 `GET /api/v1/admin/skills` 获取。

### 6.1 加载生命周期

- Runtime 初始化时扫描一次，并在本次进程生命周期内使用该快照，避免同一运行中的规则无版本地漂移；
- 管理员状态接口每次请求都会重新扫描磁盘，因此文件问题可以立即显示；
- 修改 Skill 后，管理端状态会先更新，但必须重启应用，新的 Workflow 才会进入 Runtime 回复链路。

## 7. 安全降级

只有 `READY` 和 `WARN` Skill 可以加载。`FAILED` Skill 不进入 Prompt。

两个关键场景有额外的固定安全门：

- `CONSULT` 缺少 `supportive_response_baseline` 时，ResponseAgent 不继续自由生成，改用固定安全回复；
- `RISK` 缺少 `high_risk_safety_plan` 时，改用固定高风险安全回复；
- 固定回复仍需经过 SafetyAgent 的确定性审查；
- Skill 不能降低硬规则给出的风险下限，也不能覆盖隐私、防危险细节和防虚构资源约束。

这保证 Skill 配置损坏时系统能够暴露故障，同时不把关键心理场景交给无约束生成。

## 8. 工作人员交接摘要

`counselor_handoff_summary` 与学生回复 Skill 不同，它不进入学生端回复 Prompt。`AgentHarness` 在心理报告取得 ID 后调用模板渲染，并把摘要写入 `psychological_reports.handoff_summary`。

所有 `CONSULT` 和 `RISK` 报告都保存以下字段：

```text
报告 ID / 学生身份 / 风险等级 / 情绪标签 / 置信度
模型摘要 / 建议跟进动作 / 学生原始表达摘录
```

其中：

- `CONSULT` 建议工作人员复核当前困扰、日常影响和支持需求；
- `RISK` 建议优先核实当前安全、陪伴情况和现实支持连接；
- 原始表达只截取必要长度，不添加学生未表达的事实；
- `RISK` 个案复用报告中的同一份交接摘要，避免报告与个案信息漂移；
- 应用启动时会幂等回填历史报告中缺失的交接摘要，并同步已有风险个案；
- 报告列表不直接返回交接摘要和原始表达；管理员打开报告上下文时才读取，并记录审计日志。

## 9. 维护 Workflow

新增或修改 Skill 时按以下流程操作：

1. 在 `skills/<skill_name>/SKILL.md` 新建或修改资产，保持目录名、`name` 和下划线命名一致。
2. 用 `description` 清楚说明触发场景和目标，不在其中堆叠完整规则。
3. 在 `## Workflow` 中使用短而可验证的动作规则，同时写明关键禁止项。
4. 如果新增 Skill，需要在 `REQUIRED_SKILLS` 和确定性路由中明确它是否为必需资产、何时加载；不要让 Skill 自行决定调度。
5. 在管理端确认状态和 `issues`，重启应用后验证实际 Runtime 加载结果。
6. 为校验、路由、Prompt 注入、安全回退和业务持久化补充自动化测试。

不要在 Skill 中写入未经知识库确认的电话号码、办公时间、人员姓名、学校政策或其他易变化事实。事实信息应由 RAG 上下文提供，Skill 只规定如何安全使用这些信息。

## 10. 测试与验收

Skill 专项测试位于 `tests/test_skills.py`，覆盖：

- 七个项目 Skill 全部为 `READY`；
- `WARN`、`FAILED` 和必需资产缺失状态；
- `CHAT`、`CONSULT`、`RISK` 的确定性路由；
- ContextAgent 将 Workflow 注入 ResponseAgent Prompt；
- 管理端状态接口；
- `CONSULT` 报告交接摘要的生成、保存和受控读取。

运行专项测试：

```bash
python -m pytest tests/test_skills.py -q
```

涉及路由、报告或工具链修改时，还应运行完整测试：

```bash
python -m pytest -q
```

验收时至少确认：`CHAT` 无心理 Skill 与心理副作用；`CONSULT` 生成报告、交接摘要和台账；`RISK` 创建个案，并且预警严格依赖个案创建成功。

## 11. 相关文档

- [Agent Runtime](./AI_RUNTIME.md)：黑板协议、Agent 分工、Prompt 与安全审查。
- [AgentHarness](./AGENT_HARNESS.md)：报告、交接摘要和工具计划的持久化边界。
- [RAG 模块](./RAG.md)：知识事实的检索与上下文来源。
- [MCP 与异步工具队列](./MCP_AND_TOOL_QUEUE.md)：台账、个案和预警的依赖与执行。
- [产品需求](./PRD.md)：三级风险业务规则与验收要求。
