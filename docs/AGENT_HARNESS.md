# AgentHarness 工程链路说明

> 文档版本：1.0  
> 更新日期：2026-08-08  
> 核心实现：`app/harness/agent_harness.py`  
> 独立运行入口：`app/harness/runner.py`

## 1. 定位

`AgentHarness` 是 MindNook 聊天业务用例的后端编排边界。它位于 HTTP 服务和 Agent Runtime 之间，负责会话、隐私、记忆、持久化、工具任务和事件交付；多 Agent 推理、RAG、回复生成与安全审查由 `CoordinatorAgent` 及其专业 Agent 负责。

生产聊天链路为：

```text
HTTP（认证 + SSE）
  → ChatService（只转发公开事件）
  → AgentHarness（业务编排与副作用）
  → CoordinatorAgent（多 Agent 推理）
  → UnderstandingAgent / SafetyAgent / ContextAgent / ResponseAgent
```

独立 Runner 不启动 HTTP：

```text
固定场景输入
  → 临时用户与会话
  → AgentHarness
  → 实际配置的 Model Provider
  → Agent Runtime / RAG / 安全审查
  → 临时数据库、Trace 与工具结果检查
```

## 2. 整体聊天链路

一条学生消息从浏览器进入系统，到最终安全回复返回前端，完整经过以下链路：

```text
学生前端
  ↓ POST /api/v1/conversations/{conversation_id}/messages:stream
FastAPI 鉴权与会话接口
  ↓
ChatService
  ↓ 只转发 public=true 的事件
AgentHarness
  ├─ 验证学生身份和会话归属
  ├─ 按 client_request_id 执行幂等检查
  ├─ 读取短期记忆与 Agent 私有记忆
  ├─ 对进入模型的用户内容执行 PII 脱敏
  └─ 保存原始用户消息和脱敏副本
  ↓
CoordinatorAgent.plan()
  ├─ 执行固定安全规则
  ├─ UnderstandingAgent：识别意图、主题和知识需求
  ├─ Coordinator：合并 general / psychological_support / safety_risk 路由
  ├─ SafetyAgent：评估最终风险等级
  ├─ ContextAgent
  │    ├─ 组装会话记忆
  │    ├─ 判断是否需要调用 RAG
  │    ├─ 向量召回 + BM25
  │    ├─ 归一化融合 + 本地重排
  │    └─ 第一名上下文扩展
  └─ ResponseAgent：生成最终回复 Prompt
  ↓
Model Provider 流式生成候选回复
  ↓ 候选 Token 只产生内部 model_progress，不公开给学生
AgentHarness 在服务端累计完整候选
  ↓
SafetyAgent 发送前安全审查
  ├─ 通过：Coordinator 采用候选回复
  ├─ 不通过：ResponseAgent 按审查意见修复并再次审查
  └─ 模型、预算或复审失败：采用固定安全模板
  ↓
AgentHarness
  ├─ 在同一数据库事务中保存最终助手消息
  ├─ 保存 Agent Run / Step / Collaboration Event / Trace
  ├─ 心理支持或安全场景生成 PsychologicalReport
  ├─ 将台账、个案和预警计划写成持久化 ToolJob
  └─ 提交数据库事务
  ↓ 事务提交成功后
  ├─ 更新短期记忆与 Agent 私有记忆
  └─ 唤醒后台工具 Dispatcher 处理已提交任务
  ↓
最终安全回复按 28 个字符切片
  ↓ SSE：delta / completed，异常时发送 error
学生前端
  ├─ reasoning：正在理解你的话…
  ├─ generating：正在生成回复…
  ├─ checking：正在进行安全检查…
  ├─ delta：追加最终回复文本
  ├─ completed：结束本轮回复
  └─ error：取消流读取、展示错误并恢复输入状态
```

这条链路采用“模型内部真流式、审查后公开切片流”的安全策略：Model Provider 和 Runtime 会实时接收候选 Token，但未经 SafetyAgent 审查的内容不会直接发送给学生。用户必须等待完整候选生成、安全审查和事务提交后，才会收到最终回复的公开 `delta`。该设计优先避免危险内容先流出后无法撤回，代价是首个公开回复字符的等待时间较长。

不同路由在链路中的主要差异：

| 最终路由 | RAG | 心理报告 | 工具计划 |
|---|---|---|---|
| `CHAT` | 不调用 | 不生成 | 无 |
| `CONSULT` | 调用 | 生成 | `ledger_write` |
| `RISK` | 调用并补充固定安全检索主题 | 生成 | `ledger_write + case_create + alert_send` |

### 2.1 最终结果落库与副作用

这部分不是简单地连续写几个对象，而是分为“先保存用户请求”“提交最终结果事务”“提交后更新缓存与分发工具”三个阶段。

#### 阶段一：先保存用户请求

进入 Runtime 前，Harness 已经创建学生消息并提交一次数据库事务：

```text
Message(sender_type="student")
  content            = 用户原文
  redacted_content   = 脱敏后文本
  client_request_id  = 客户端幂等键
  status             = generating
```

这样即使后续模型调用耗时或失败，系统仍能知道该请求已经被接受，也能阻止相同 `client_request_id` 被重复执行。若同一请求再次到达但助手回复尚未生成，Harness 返回 `CHAT_REQUEST_IN_PROGRESS`。

#### 阶段二：提交最终结果事务

模型候选通过安全审查或被安全模板替代后，Harness 在同一个数据库事务中组织以下数据。

1. 最终助手消息

```text
Message(sender_type="assistant")
  content             = SafetyAgent 审查后最终回复
  redacted_content    = 最终回复
  reply_to_message_id = 本轮学生消息
  status              = completed
```

同时把学生消息的 `status` 从 `generating` 更新为 `completed`。公开 SSE 返回的是这条已经通过审查的最终消息，不是模型原始候选流。

2. Agent Run 与步骤

`AgentRun` 保存一轮协作的总体摘要：

```text
run_id / conversation / source_message
status / route / final_risk_level
provider / model_calls / degraded / total_latency_ms
```

每个本地或模型步骤写入 `AgentStep`，包括步骤名、成功或失败状态、模型、延迟、Token 估算、摘要和错误码。管理员可以据此区分意图识别、风险评估、回复生成、安全审查、修复或降级发生在哪一步。

3. 协作事件与完整 Trace

Blackboard 中只追加不覆盖的协作事件会写入 `AgentCollaborationEvent`，记录：

```text
sequence / event_type / actor / task_id / payload
```

`AgentTraceService` 还会持久化本轮完整运行快照，包括原始与脱敏输入、记忆来源、计划、最终结果和协作统计。`runtime_event`、`runtime_plan`、`trace` 属于内部诊断信息，不直接发送给学生。

4. PsychologicalReport

只有最终 `risk_level` 不是 `CHAT` 时才创建心理报告。报告关联学生、会话和来源消息，并保存：

```text
emotion_summary
risk_level
confidence
reason_summary
handoff_summary
```

`handoff_summary` 由 `counselor_handoff_summary` Skill 的固定模板生成，包含报告 ID、学生身份、风险等级、情绪标签、置信度、模型摘要、跟进建议和必要的原始表达摘录。它仅供工作人员查看，不进入学生端回复 Prompt；历史缺失摘要在应用启动时幂等回填。

普通问答不会生成心理报告，也不会进入心理风险工具链。

5. ToolJob 与任务 DAG

Harness 根据最终风险生成工具计划，并通过 `enqueue_report_jobs()` 将计划写为数据库中的持久化任务：

```text
CHAT    → 无
CONSULT → ledger_write
RISK    → ledger_write + case_create + alert_send
```

此时只会幂等创建 `ToolJob(status="PENDING")`，并不会在数据库事务内部写 Excel、创建个案或发送预警。高风险的 `ALERT_SEND` 通过 `depends_on_job_id` 依赖 `CASE_CREATE`。这样可以避免数据库事务回滚后已经产生外部副作用。

上述助手消息、Trace、心理报告和后台任务全部准备完成后，Harness 才执行 `db.commit()`。因此工具 Worker 只能读取已经提交并具有完整报告关联的数据。

需要注意，学生原始消息在 Runtime 调用前已经单独提交，所以这里的原子性范围是“最终助手消息 + Run/Trace + 心理报告 + ToolJob”，不是把整轮从接收请求开始都放在一个长事务里。

#### 阶段三：提交后的记忆与工具处理

数据库提交成功后，Harness 才执行：

1. 把学生消息和最终助手消息追加到 Redis 短期记忆；
2. 按 Agent 隔离规则写入 Agent 私有记忆；
3. 唤醒 Tool Dispatcher 处理数据库中已提交的 `pending` 任务；
4. 将最终安全回复作为公开 `delta` 和 `completed` 事件发送给学生。

短期记忆是缓存，数据库消息是可回填的事实来源。因此记忆更新位于数据库事务之外，不应反向破坏已经成功落库的聊天结果。工具任务同样在事务提交后执行：测试和独立 Harness 使用同步 `InlineToolDispatcher`，普通应用环境使用进程内 `ThreadedToolDispatcher`。后者只被 Harness 唤醒，不在流式请求线程内执行工具；完整的领取、线程池、重试、限流、死信和 MCP 说明见 [MCP_AND_TOOL_QUEUE.md](./MCP_AND_TOOL_QUEUE.md)。

当前三个工具的实际效果是：

| 工具 | 当前行为 |
|---|---|
| `ledger_write` | 写入可重建的风险台账数据，并生成或更新 Excel 台账 |
| `case_create` | 为中高风险报告创建 `RiskCase` 和初始个案事件 |
| `alert_send` | 创建 `AlertDelivery`；SMTP 配置完整时发送邮件，否则使用 `log` 渠道留痕 |

#### 失败处理

- 模型生成失败或超出 Agent 预算：Runtime 采用固定安全模板，标记 `degraded=true`，仍可按成功结果落库。
- 会话不存在或不属于当前学生：回滚并返回 `CHAT_CONTEXT_NOT_FOUND`，不会创建本轮消息。
- 最终落库或业务处理发生未预期异常：回滚当前事务，将已存在的学生消息标记为 `failed`，并返回可重试的 `CHAT_PROCESSING_FAILED`。
- 工具任务失败：任务记录错误并按指数延迟回到 `PENDING`；累计达到最大次数后进入 `DEAD` 并写入 `dead_letter_records`，不会撤销已经发送给学生的最终回复和心理报告。

## 3. Harness 与 Runtime 的职责边界

### AgentHarness 负责

- 校验学生和会话归属；
- 使用 `client_request_id` 保证请求幂等；
- 读取短期记忆和各 Agent 私有记忆；
- 对进入模型的用户消息执行 PII 脱敏；
- 调用 Runtime 的 `plan()`、`stream_response()` 和 `finalize_response()`；
- 保存学生消息、助手消息、Agent Run、步骤、协作事件和完整 Trace；
- 为心理支持与风险对话生成心理报告；
- 根据风险等级生成台账、个案和预警工具计划；
- 提交事务后分发工具任务；
- 区分公开事件和内部诊断事件；
- 将异常转换为稳定的业务错误。

### Agent Runtime 负责

- 意图识别与路由合并；
- 风险评估和固定安全规则兜底；
- ContextAgent 的记忆、RAG 和 Skill 上下文组装；
- 回复生成；
- 输出安全审查、修复和最终采用；
- 生成任务、Artifact、Message、Event 和步骤 Trace。

Runtime 不直接访问数据库，不创建心理报告，也不执行台账、个案或预警等业务工具。

## 4. 单轮完整执行流程

`AgentHarness.run(ChatCommand)` 的执行顺序如下：

1. 按学生 ID 和会话 ID 解析上下文，并验证会话归属。
2. 按 `client_request_id` 检查重复请求：
   - 已有完整回复时重放公开结果；
   - 请求仍在处理时返回 `CHAT_REQUEST_IN_PROGRESS`。
3. 发布 `accepted` 事件。
4. 从 Redis 或降级存储读取短期记忆及 Agent 私有记忆。
5. 保存用户原文，同时生成仅供模型使用的脱敏文本。
6. 调用 `runtime.plan()`：
   - UnderstandingAgent 发布意图；
   - SafetyAgent 发布风险评估；
   - ContextAgent 按最终路由决定是否调用 RAG；
   - ResponseAgent 组装回复消息；
   - Runtime 发布内部任务、Artifact 和协作事件。
7. 调用 `runtime.stream_response()` 获取模型候选回复。
8. 调用 `runtime.finalize_response()` 执行安全审查并采用或修复回复。
9. 模型调用失败或超出 Agent 预算时调用 `generation_failed()` 生成安全降级结果。
10. 保存助手消息、Agent Run、Agent Step、协作事件和完整 Trace。
11. 非普通对话生成 `PsychologicalReport`。
12. 根据最终风险生成工具计划并入队：
    - 普通问答：无工具；
    - 低风险心理支持：`ledger_write`；
    - 中风险：`ledger_write + case_create`；
    - 高风险：`ledger_write + case_create + alert_send`。
13. 提交数据库事务，更新短期记忆和 Agent 私有记忆。
14. 分发工具任务，并发布 `delta`、`completed` 和内部 `trace` 事件。

## 5. 输入和事件协议

### ChatCommand

```text
student_public_id
conversation_public_id
content
client_request_id
```

### 公开事件

公开事件可以由 `ChatService` 转发给 SSE 客户端：

| 事件 | 含义 |
|---|---|
| `accepted` | 请求已接收，并标明是否为重复请求 |
| `status` | `reasoning`、`generating`、`checking` 等公开阶段 |
| `delta` | 最终安全回复的分片 |
| `completed` | 回复消息和会话标识 |
| `error` | 稳定的用户可见错误 |

### 内部事件

以下事件默认 `public=false`，不得直接发送给学生：

| 事件 | 含义 |
|---|---|
| `runtime_event` | Agent 协作事件时间线 |
| `runtime_plan` | 意图、风险、知识数量、任务和记忆摘要 |
| `model_progress` | 内部候选流的接收进度 |
| `trace` | 路由、风险、降级、工具计划、步骤和协作统计 |

Provider 的候选 Token 不会直接作为公开 `delta` 输出。Harness 只在 SafetyAgent 完成审查、Runtime 采用最终回复并成功落库后，才将安全成品分片发送给客户端。

## 6. Model Provider 与真实 API

Harness 不固定使用 Mock 或真实模型，实际 Provider 由配置决定：

```text
MODEL_PROVIDER=mock       → MockModelProvider，不访问网络
MODEL_PROVIDER=bailian    → OpenAICompatibleModelProvider，调用配置的模型 API
```

单元测试通过依赖注入使用 `RecordingModelProvider(MockModelProvider)`，因此确定、离线且不消耗 API Token。

Engineering Runner 固定使用 `MockModelProvider`，以保证离线、确定、无需 API Token。
真实模型连通性与回答质量不属于一键工程回归，需另行执行 Provider 冒烟和人工评审。

## 7. 独立 Runner

Runner 提供六类固定套件：

| 套件 | 验证链路 |
|---|---|---|
| `risk-safety` | 高风险识别、报告、元数据隔离和工具入队 |
| `agent-routing` | CHAT / CONSULT / RISK 与 Agent 步骤 |
| `standard-skills` | 标准 Skill 校验、选择和 Workflow 注入 |
| `rag` | 100 条评测集与召回指标 |
| `api` | 健康、认证授权、SSE、管理员知识库接口 |
| `tool-queue` | Excel、case、alert DAG、幂等、限流和死信 |

运行方式：

```bash
python -m app.harness.runner
python -m app.harness.runner --json
python -m app.harness.runner --suite risk-safety --suite tool-queue
```

每个套件独立使用临时 SQLite、内存短期记忆、临时 RAG 索引和临时 Excel，
不污染正式环境。JSON 输出包含：

```text
ok
environment
summary
suites[].passed
suites[].duration_ms
suites[].checks[].metrics
```

任一套件失败时退出码为非零，可直接作为 CI 门禁。

## 8. RAG 在 Harness 中的范围

当最终评估属于 `psychological_support`、`safety_risk`，或风险等级为 `medium/high` 时，ContextAgent 会调用完整的 Hybrid RAG：

```text
向量召回 + BM25
  → 分数归一化与融合
  → 本地重排
  → 第一名前后文扩展
  → ResponseAgent Prompt
```

因此 Harness 能验证：

- RAG 是否按路由触发；
- 索引、召回和上下文注入是否能完成；
- RAG 异常是否降级为内置关键词知识；
- RAG 上下文是否贯穿到回复与 Trace。

Engineering Harness 会运行 100 条 RAG 评测集，并报告 Hit@1、Recall@3、MRR@3、
nDCG@3、安全召回与重复率。它使用稳定工程回归底线；更严格的发布验收仍单独运行：

```bash
python -m app.rag.evaluation --evaluate-answers
python -m app.rag.evaluation --evaluate-answers --strict
```

因此两层门禁的职责是：

```text
Engineering Harness：可重复的一键工程回归和召回指标底线
RAG Evaluation --strict：更高阈值的发布级检索验收
```

## 9. 自动测试覆盖

`tests/test_agent_harness.py` 当前覆盖：

- 不依赖 HTTP 的高风险完整业务链；
- 手机号进入模型前被替换为 `[PHONE]`；
- 消息、Run、Step、协作事件、Trace 和心理报告落库；
- 高风险台账、个案、预警任务和 Excel 文件生成；
- Runtime Plan 只产生推理结果，不提前执行交付副作用；
- ChatService 只暴露公开事件；
- 相同 `client_request_id` 的幂等重放；
- 会话不存在时返回稳定错误；
- 第二轮会话读取短期记忆。

运行 Harness 测试：

```bash
python -m pytest tests/test_agent_harness.py -q
```

运行所有测试：

```bash
python -m pytest -q
```

## 10. 完整性边界

当前 Harness 对“后端单轮聊天业务用例”是完整的，但不是部署级端到端 E2E。

| 能力 | 当前状态 |
|---|---|
| 会话、脱敏、记忆、Agent Runtime | 已覆盖 |
| RAG 触发、注入与降级 | 已覆盖 |
| 回复生成与输出安全审查 | 已覆盖 |
| 消息、报告、Trace 和工具计划 | 已覆盖 |
| 幂等与稳定业务错误 | 已覆盖 |
| 真实模型 API 冒烟 | 不属于确定性 Engineering Harness，需另行验证 |
| RAG 排名与证据质量 | Engineering Harness 回归底线 + 独立严格评测 |
| 回答同理心、事实性和建议质量 | 尚需批量真实模型评测与人工盲审 |
| HTTP、认证和 SSE 协议 | API Harness 已覆盖 TestClient 链路 |
| 真实 MySQL、Redis、异步 Worker | Runner 使用临时数据库和进程内工具执行器 |
| 浏览器和前端 UI | 未覆盖 |
| 并发、压测、故障注入和长时间稳定性 | 未形成独立 Harness |

## 11. 推荐验收组合

一次较完整的工程验收建议依次执行：

```bash
# 1. 单元与集成测试
python -m pytest -q

# 2. 六类一键工程验证
python -m app.harness.runner

# 3. RAG 检索质量
python -m app.rag.evaluation --evaluate-answers --strict
```

如果目标是上线验收，还需要补充真实基础设施、HTTP/SSE、异步 Worker、浏览器 UI 和人工回答质量评审；不能只凭 Harness 的 `ok=true` 判断系统已经达到生产质量。
