# MindNook 分层记忆与上下文压缩设计

> 版本：1.0  
> 更新日期：2026-08-01  
> 实现目录：`app/memory/`

## 1. 目标

记忆模块在保持最近对话细节的同时限制 Prompt 大小，并明确区分：

- 会话热缓存。
- 业务事实数据。
- Prompt 摘要和最近原文窗口。
- Agent 私有状态。
- 仅用于审计的 Trace memory。

Agent Runtime 不直接访问 Redis 或 MySQL。AgentHarness 在运行前构造 MemoryContext，运行后写回缓存、Agent 私有记忆和审计 Trace。

## 2. 分层结构

```mermaid
flowchart TD
    Harness[AgentHarness] --> Redis{Redis 短期记忆命中?}
    Redis -->|是| Window[最多40条脱敏消息]
    Redis -->|否| MySQL[(MySQL chat_messages)]
    MySQL --> Backfill[最近40条回填Redis]
    Backfill --> Window
    Window --> Compress[ContextCompressor]
    Compress --> Brief[memoryBrief]
    Compress --> History[最近8条 modelHistory]
    Brief --> Runtime[CoordinatorAgent / Blackboard]
    History --> Runtime
    Private[Agent 私有 Redis keys] --> Runtime
    Runtime --> Trace[(agent_run_traces 审计记忆)]
```

## 3. 会话短期记忆

Redis key：

```text
mindbridge:short-term-memory:<session_public_id>
```

默认策略：

| 配置 | 默认值 |
|---|---:|
| 最大消息数 | 40 |
| TTL | 86400 秒 |
| 最近原文数 | 8 |
| memoryBrief 最大字符 | 1200 |
| modelHistory 最大字符 | 6000 |

Redis list 中每条记录包含：

```json
{
  "message_id": "uuid",
  "role": "student",
  "content": "脱敏内容",
  "created_at": "ISO-8601"
}
```

每次写入执行 `RPUSH + LTRIM + EXPIRE`。每次成功读取刷新 TTL。Redis 中不保存原始手机号、邮箱或身份证号。

## 4. MySQL 完整记录与回填

完整学生和助手消息保存在 `chat_messages`，数据库是事实数据源。

加载流程：

1. 尝试读取 Redis list。
2. Redis 有有效记录时直接使用并刷新 TTL。
3. Redis key 不存在、内容损坏或 Redis 不可用时，从数据库查询当前会话最近 40 条 `completed` 消息。
4. 按时间恢复正序并回填 Redis。
5. Redis 故障不阻断聊天，直接使用数据库结果。

当前项目通过 SQLAlchemy 同时兼容 MySQL 和测试用 SQLite。

## 5. memoryBrief

超过最近 8 条的较早历史不会完整进入 Prompt。ContextCompressor 生成受限长度的摘录式摘要：

- 优先保留学生消息中的风险、强度、持续性、时间和求助相关词。
- 同时保留靠近最近窗口的若干历史，减少对话跳变。
- 所有内容已经脱敏。
- 默认最大 1200 字符。
- 使用摘录式而不是生成式摘要，避免额外模型调用和摘要编造。

示例：

```text
较早对话摘要（脱敏、摘录式）：学生：最近每天都睡不着；助手：建议记录担忧并联系正式支持；学生：今晚想联系同学求助
```

memoryBrief 是 Agent 和 Trace 的内部上下文，不作为学生可见心理结论。

## 6. modelHistory

最近 8 条消息保留角色和脱敏原文：

```json
[
  {"role": "user", "content": "最近考试压力很大"},
  {"role": "assistant", "content": "听起来你承受了不少压力……"}
]
```

裁剪规则：

- 从最新消息向前选择，最多 8 条。
- 默认总字符不超过 6000。
- 超长单条消息同时保留开头和结尾，中间用省略号标记。
- 不再把不同角色内容简单拼接，避免模型混淆学生和助手表达。

Runtime 中 UnderstandingAgent、SafetyAgent 和 ResponseAgent 都能看到 memoryBrief 与 modelHistory。

## 7. Agent 私有记忆

Redis key：

```text
agent:<AgentName>:<session_public_id>
```

默认最多 20 条，TTL 86400 秒。

| Agent | 保存内容 |
|---|---|
| UnderstandingAgent | 近期 intent 判断 |
| SafetyAgent | 近期风险与安全审查结果 |
| ContextAgent | 检索数量和使用的 Skill |
| ResponseAgent | 路由、风险和最终采纳策略 |

AgentHarness 一次性读取各 key，但 Runtime 通过 `private_for(agent_name)` 只把对应私有记忆注入该 Agent 的 Prompt。ResponseAgent 不会收到 SafetyAgent 的历史后台判断。

私有记忆只能作为可能过期的历史参考，不能覆盖当前输入、固定高风险规则或本轮 Safety 审查。

## 8. Trace 记忆

`AgentTraceService` 将每轮运行写入 `agent_run_traces`：

- 原始输入。
- 脱敏输入。
- memoryBrief。
- modelHistory。
- Agent steps。
- 检索结果。
- 最终 Assessment JSON。
- CollaborationBlackboard events。
- Tasks、Artifacts 和 AgentMessages。

Trace memory 永远不会作为下一轮 Prompt 输入。管理员读取会产生审计日志：

```text
GET /api/v1/admin/agent-runs/{run_id}/trace
```

协作事件也可以单独查询：

```text
GET /api/v1/admin/agent-runs/{run_id}/events
```

## 9. Docker 与本地后端

Compose 只运行 MySQL 和 Redis：

```bash
docker compose up -d mysql redis
```

Python 后端在宿主机运行：

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

连接地址使用宿主机端口：

```text
DATABASE_URL=mysql+pymysql://mindnook:mindnook-local@127.0.0.1:3307/mindnook
MYSQL_HOST_PORT=3307
REDIS_URL=redis://127.0.0.1:6379/0
```

## 10. 降级行为

| 故障 | 行为 |
|---|---|
| Redis 连接失败 | 从 MySQL 加载最近 40 条；本轮继续 |
| Redis 内容损坏 | 删除损坏 key，回填数据库记录 |
| 数据库无历史 | 使用空 MemoryContext |
| 历史超过窗口 | 较早记录进入 memoryBrief，最近 8 条保留原文 |
| Agent 私有 Redis 失败 | 使用空私有记忆，不影响主链路 |
| Trace 落库失败 | 当前实现随主业务事务回滚，避免出现不可审计的成功运行 |

## 11. 自动化测试

`tests/test_memory.py` 覆盖：

- Redis miss 后从数据库回填最近 40 条。
- 第二次读取命中 Redis。
- TTL 刷新和 40 条 LTRIM。
- Redis 故障时数据库降级。
- memoryBrief 长度和重要表达保留。
- 最近 8 条带角色 modelHistory 及字符预算。
- Agent 私有 key 隔离和条数限制。
- 私有记忆只进入所属 Agent Prompt。

API/Harness 测试还覆盖二轮会话 Redis 命中及完整 agent_run_traces 落库。
