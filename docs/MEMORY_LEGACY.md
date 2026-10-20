# MindNook 升级前记忆实现（归档）

> 本文仅记录升级前实现。当前实现见 [MEMORY.md](./MEMORY.md)。`MEMORY_ENABLED=false` 时保留旧窗口策略用于兼容回退。

> 文档版本：2.1
> 更新日期：2026-08-24
> 实现目录：`app/memory/`、`app/agents/runtime.py`、`app/harness/agent_harness.py`

## 1. 模块目标

上下文管理模块负责在保留近期对话细节的同时限制模型 Prompt 大小，并将不同用途的记忆明确隔离。

核心目标包括：

- 短期上下文优先从 Redis 读取，缺失或不可用时由当前 SQLAlchemy 业务数据库回填（默认 SQLite，可配置为 MySQL）。
- 完整聊天记录长期保存在业务数据库，不依赖 Redis 作为事实数据源。
- 对长历史执行窗口裁剪和确定性摘要压缩，避免 Prompt 随对话轮次无限增长。
- 保留最近对话的脱敏原文，避免摘要丢失语气、时间、犹豫、求助方式和风险强度。
- 隔离不同 Agent 的内部工作记忆，避免后台判断污染学生端表达。
- 将每轮运行所需的审计信息写入 Trace，但不让审计数据回流到后续 Prompt。

本模块不会把当前会话的完整历史消息全部放入模型上下文。

## 2. 总体架构

```mermaid
flowchart TD
    Input[当前用户输入] --> Redact[PII 脱敏]
    Harness[AgentHarness] --> Redis{Redis 短期记忆命中?}
    Redis -->|是| Window[最近最多 40 条脱敏消息]
    Redis -->|否或不可用| DB[(业务数据库 chat_messages)]
    DB --> Backfill[查询最近 40 条并回填 Redis]
    Backfill --> Window
    Window --> Compress[ContextCompressor]
    Redact --> Compress
    Compress --> Brief[memoryBrief 最多 500 字]
    Compress --> History[modelHistory 最近 8 条]
    Brief --> Runtime[Agent Runtime]
    History --> Runtime
    Private[Agent 私有 Redis 记忆] --> Runtime
    Runtime --> Response[生成与安全审查]
    Response --> Persist[消息与私有记忆写回]
    Runtime --> Trace[(agent_run_traces 审计记忆)]
```

Agent Runtime 不直接访问 Redis 或 MySQL。`AgentHarness` 在运行前完成会话解析、脱敏和 `MemoryContext` 构造，在运行成功后写回消息、Agent 私有记忆和 Trace。

## 3. 分层记忆机制

### 3.1 会话短期记忆

Redis Key 格式：

```text
mindbridge:short-term-memory:<session_public_id>
```

默认策略：

| 配置 | 默认值 |
|---|---:|
| 最大缓存消息数 | 40 |
| TTL | 86400 秒 |
| 最近原文窗口 | 8 条 |
| memoryBrief 最大长度 | 500 字符 |
| modelHistory 最大长度 | 6000 字符 |

Redis 使用 List 保存消息。每次写入执行 `RPUSH + LTRIM + EXPIRE`，每次成功读取都会刷新 TTL。

单条缓存记录结构：

```json
{
  "message_id": "uuid",
  "role": "student",
  "content": "脱敏后的消息内容",
  "created_at": "ISO-8601"
}
```

### 3.2 业务数据库完整记录

学生和助手消息均写入当前 SQLAlchemy 业务数据库的 `chat_messages` 表；默认配置使用 SQLite，`.env.example` 使用 MySQL。业务数据库保存完整记录，是事实数据源；Redis 只是最近消息的热缓存。

加载流程：

1. 根据 `session_public_id` 读取 Redis List。
2. Redis 命中时直接使用最近最多 40 条并刷新 TTL。
3. Redis Key 缺失、内容损坏或 Redis 不可用时，查询当前会话最近 40 条 `completed` 消息。
4. 将数据库结果恢复为时间正序，并尝试回填 Redis。
5. Redis 故障不会阻断聊天，本轮继续使用数据库结果。

数据库中的完整记录不会因此被删除；但模型本轮可见的会话记忆仍以最近 40 条为上限。
> 实现说明：`ShortTermMemoryService.load()` 的 `MemoryContext.source` 在数据库回填路径上仍返回历史兼容值 `"mysql"`，即使当前 `DATABASE_URL` 指向 SQLite；该字段表示“数据库回填”，不能据此判断实际数据库方言。

### 3.3 内部摘要记忆

`memoryBrief` 是 Agent 和 Trace 使用的内部背景，不是面向学生的心理结论，也不是模型自由生成的总结。

当历史超过最近原文窗口时，较早历史通过固定规则压缩，减少额外模型调用和摘要编造风险。

### 3.4 最近原文窗口

`modelHistory` 默认保留最近 8 条脱敏消息，并保留原始角色关系：

```json
[
  {"role": "user", "content": "最近考试压力很大"},
  {"role": "assistant", "content": "听起来你承受了不少压力……"}
]
```

摘要可以节省上下文，但可能丢失原话中的风险强度和语气。因此最近消息仍以脱敏原文进入模型。

### 3.5 Agent 私有记忆

不同 Agent 使用隔离的 Redis Key：

```text
agent:<AgentName>:<session_public_id>
```

默认每个 Key 最多保存 20 条，TTL 为 86400 秒。构造单个 Agent 的 Prompt 时，只读取该 Agent 最近 4 条私有记录。

| Agent | 主要保存内容 |
|---|---|
| UnderstandingAgent | 最近的意图和话题判断 |
| SafetyAgent | 风险评估与安全审查结果 |
| ContextAgent | 检索数量和上下文策略 |
| ResponseAgent | 路由、风险等级和回复采纳策略 |

私有记忆的关键价值是隔离：

- SafetyAgent 的 `risk`、`confidence` 和审查结果不会直接成为学生端话术。
- ResponseAgent 的表达策略不会影响 UnderstandingAgent 的意图识别。
- 私有记忆只能作为历史参考，不能覆盖当前输入、固定高风险规则或本轮安全审查。

### 3.6 Trace 审计记忆

`AgentTraceService` 将每轮 Agent Run 写入 `agent_run_traces`，包括：

- 原始输入和脱敏输入。
- `memoryBrief` 和 `modelHistory`。
- Agent Steps 和模型调用信息。
- 检索结果和最终 Assessment JSON。
- CollaborationBlackboard 事件。
- Tasks、Artifacts 和 Agent Messages。

Trace 用于管理员和开发者复盘本轮判断过程。它不会作为下一轮模型 Prompt 的输入。

管理员可以分别读取完整 Trace 和协作事件：

```text
GET /api/v1/admin/agent-runs/{run_id}/trace
GET /api/v1/admin/agent-runs/{run_id}/events
```

## 4. 上下文压缩算法

### 4.1 输入脱敏

历史消息和当前输入在进入压缩器前执行 PII 脱敏。目前支持替换：

- 手机号：`[PHONE]`
- 邮箱：`[EMAIL]`
- 身份证号：`[ID_CARD]`
- 疑似学号：`[STUDENT_ID]`

Redis 反序列化和 `ContextCompressor` 内部也会再次脱敏，避免旧缓存或直接调用绕过保护。

### 4.2 历史窗口裁剪

```text
会话完整历史：业务数据库持久化
本轮可加载历史：最近最多 40 条
模型最近原文：最近最多 8 条
旧历史：最近 40 条中位于原文窗口之前的部分
```

`modelHistory` 还受 6000 字符总预算限制。选择顺序从最新消息向前；单条内容过长时保留开头和结尾，中间使用省略号。

### 4.3 memoryBrief 规则

摘要采用固定的 4/3/1 结构：

1. 学生近期关注：从旧历史中取最近最多 4 条学生消息，每条最多 80 字。
2. 系统已经提供的支持：从旧历史中取最近最多 3 条助手回复，每条最多 70 字。
3. 本轮输入关注：取当前脱敏输入，最多 80 字。
4. 最终摘要默认最多 500 字。

由于 `4 × 80 + 3 × 70 + 80` 加上栏目标题可能超过 500 字，压缩器会先为三个栏目保留固定结构，再在学生和助手栏目内部平均分配剩余字符预算。这样不会因为整体截断而丢失栏目标题或本轮输入。

示例：

```text
历史记忆摘要（脱敏、确定性摘录）：
学生近期关注：最近每天都睡不着；考试临近时很难集中
系统已经提供的支持：建议拆分眼前任务；建议联系可信任的人
本轮输入关注：今晚还是睡不着，想知道现在能先做什么
```

### 4.4 长短历史策略

当历史不超过 8 条时：

- `modelHistory` 保留完整的脱敏短历史，仍受 6000 字符安全上限约束。
- `memoryBrief` 可以写入 Trace，但不重复注入模型 Prompt。

当历史超过 8 条时：

- 最近 8 条保留为角色化脱敏原文。
- 最近窗口之前的旧历史生成 `memoryBrief`。
- `memoryBrief` 作为独立 `system` 消息进入模型上下文。

## 5. Prompt 组装方式

### 5.1 短历史

```text
system: Agent 系统指令和该 Agent 的私有记忆
user/assistant: 不超过 8 条的脱敏历史原文
user: 当前脱敏输入
```

### 5.2 长历史

```text
system: Agent 系统指令和该 Agent 的私有记忆
system: 旧历史 memoryBrief，仅作为内部背景参考
user/assistant: 最近 8 条脱敏历史原文
user: 当前脱敏输入
```

UnderstandingAgent、SafetyAgent 和 ResponseAgent 均使用该角色化历史序列。ResponseAgent 还会获得本轮意图、风险判断、RAG 知识、适用 Skill 和回复硬约束。

## 6. 为什么不使用全量历史

### 6.1 控制成本与延迟

全量历史会使 Token 数随对话持续增长，增加请求延迟、推理成本和超出模型上下文窗口的风险。

### 6.2 减少旧话题干扰

学生可能在同一会话中讨论学习、生活、情绪和技术问题。旧消息无差别进入模型，容易让过期话题干扰本轮路由和判断。

### 6.3 提高当前轮稳定性

长上下文中存在大量相似或冲突信息时，模型可能抓错重点。有限窗口和结构化摘要让当前输入保持最高优先级。

### 6.4 保留风险原话

纯摘要可能把“我不想活了”压缩成“学生压力较大”，丢失风险强度。系统只压缩旧历史，最近消息继续保留脱敏后的原文。

## 7. 写回与运行时序

单轮请求的主要顺序如下：

1. 解析学生和会话。
2. 对当前输入执行 PII 脱敏。
3. 从 Redis 或业务数据库加载最近历史并构造 `MemoryContext`。
4. 加载各 Agent 的隔离私有记忆。
5. 将当前学生消息以 `generating` 状态写入数据库，再启动 Runtime。
6. Runtime 完成意图识别、安全评估、上下文检索、回复生成和安全审查。
7. 将学生消息更新为 `completed`，写入助手消息，并在同一最终事务中写入 Trace。
8. 最终数据库事务提交成功后，将两条完成消息追加到 Redis，并刷新 TTL、裁剪至 40 条。
9. 写入各 Agent 本轮私有记忆。

当前用户输入不会提前混入历史窗口，而是作为本轮最后一条 `user` 消息单独传入模型。

## 8. 配置项

```text
SHORT_TERM_MEMORY_PREFIX=mindbridge:short-term-memory
SHORT_TERM_MEMORY_MAX_MESSAGES=40
SHORT_TERM_MEMORY_TTL_SECONDS=86400
MEMORY_RECENT_MESSAGES=8
MEMORY_BRIEF_MAX_CHARS=500
MEMORY_MODEL_HISTORY_MAX_CHARS=6000
AGENT_PRIVATE_MEMORY_MAX_ENTRIES=20
AGENT_PRIVATE_MEMORY_TTL_SECONDS=86400
```

对应代码默认值位于 `app/core/config.py`，部署时可以通过环境变量覆盖。

## 9. 本地运行

Docker Compose 只负责 MySQL 和 Redis，Python 后端在宿主机运行：

```bash
docker compose up -d mysql redis
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

使用 Compose 的默认宿主机端口时，连接配置为：

```text
DATABASE_URL=mysql+pymysql://mindnook:mindnook-local@127.0.0.1:3307/mindnook
MYSQL_HOST_PORT=3307
REDIS_URL=redis://127.0.0.1:6379/0
```

不启动 MySQL 时，可以沿用项目默认 SQLite 配置；Redis 不可用时会按下一节的规则降级，不阻断基础聊天。

## 10. 降级与故障处理

| 故障场景 | 系统行为 |
|---|---|
| Redis Key 不存在 | 从业务数据库加载最近 40 条并回填 |
| Redis 内容损坏 | 删除损坏 Key，回退数据库 |
| Redis 连接失败 | 直接使用数据库结果，本轮继续 |
| 数据库没有历史 | 使用空历史和当前输入继续 |
| Agent 私有 Redis 失败 | 使用空私有记忆，不阻断主链路 |
| RAG 不可用 | 使用内置知识降级，不阻断回复 |
| Trace 写入失败 | 当前事务回滚，避免出现不可审计的成功运行 |

## 11. 当前边界

- 业务数据库保存完整聊天记录，但本轮上下文加载和摘要只覆盖最近 40 条；更早消息不会进入 `memoryBrief`。
- 当前摘要是确定性摘录，不是实体记忆、用户画像或长期滚动语义摘要。
- `modelHistory` 和 `memoryBrief` 使用字符预算，不是模型 Tokenizer 的精确 Token 预算。
- ContextAgent 会保存隔离的检索策略记录；当前确定性检索流程不会把其他 Agent 的私有状态用于检索。
- 私有记忆 TTL 到期后会丢失，但数据库消息和 Trace 不受该 TTL 影响。

## 12. 代码映射

| 职责 | 文件 |
|---|---|
| 配置默认值 | `app/core/config.py` |
| MemoryContext 和 MemoryMessage | `app/memory/types.py` |
| Redis/业务数据库加载与上下文压缩 | `app/memory/service.py` |
| Redis Store 和故障熔断 | `app/memory/store.py` |
| Agent 私有记忆 | `app/memory/private.py` |
| Trace 审计记忆 | `app/memory/trace.py` |
| 请求编排、脱敏和写回 | `app/harness/agent_harness.py` |
| 模型 Prompt 组装 | `app/agents/runtime.py` |
| 完整消息与 Trace 表模型 | `app/models.py` |
| 自动化测试 | `tests/test_memory.py` |

## 13. 验证要求

上下文模块的自动化测试至少覆盖：

- Redis Miss 后从业务数据库回填最近 40 条。
- 第二次读取命中 Redis，并刷新 TTL。
- Redis 写入裁剪到 40 条。
- Redis 故障时数据库降级。
- 4/3/1 摘要结构、PII 脱敏和 500 字上限。
- `modelHistory` 最近 8 条和 6000 字符预算。
- 短历史不注入重复摘要。
- 长历史以 `system` 摘要、角色化原文和当前输入的顺序进入模型。
- Agent 私有 Key 隔离和条数限制。
- 私有记忆只进入所属 Agent 的 Prompt。
- Trace 持久化且不会回流到后续 Prompt。
