# MCP 与异步工具队列

## 1. 目标与边界

聊天主链路只负责生成并提交心理报告和工具任务，不直接执行 Excel、个案或邮件操作。生产环境的 `ThreadedToolDispatcher` 在提交后被唤醒，学生端 SSE 可以继续输出；工具执行失败不会回滚已经提交的助手消息和心理报告。

测试环境和独立 Harness 使用 `InlineToolDispatcher`，以便断言完整业务结果。它与生产 Worker 调用相同的业务操作，但会在当前测试流程中同步排空到期任务。

```text
Agent 生成 PsychologicalReport
        ↓ 同一数据库事务
写入 tool_jobs（PENDING）
        ↓ commit 后仅唤醒 Worker
Dispatcher 每秒轮询到期任务
        ↓
Excel 线程池 / 个案线程池 / 预警线程池
        ↓
SUCCESS ── 或指数退避重试 ──→ DEAD + dead_letter_records
```

## 2. 风险任务 DAG

| 最终风险 | 创建的任务 | 依赖关系 |
|---|---|---|
| low | `EXCEL_REPORT` | 无 |
| medium | `EXCEL_REPORT`、`CASE_CREATE` | 两者可并行 |
| high | `EXCEL_REPORT`、`CASE_CREATE`、`ALERT_SEND` | `ALERT_SEND.depends_on_job_id = CASE_CREATE.id` |

`CASE_CREATE` 的优先级高于 `ALERT_SEND`，但正确性不依赖优先级：只有依赖任务进入 `SUCCESS`，预警任务才可被领取。如果依赖任务进入 `DEAD`，下游任务也会进入死信并记录依赖失败原因。

## 3. 持久化状态

`tool_jobs` 保存：

- `job_type`、`payload_json` 和全局唯一的 `idempotency_key`；
- `status`：`PENDING / RUNNING / SUCCESS / DEAD`；
- 数据库列 `attempts`（ORM 属性 `attempt_count`）、`max_attempts`；
- `run_after`、`depends_on_job_id`、`priority`；
- `locked_by`、`locked_at`、`last_error`；
- 创建和更新时间。

报告任务使用下列幂等键：

```text
excel_report:{report_public_id}
case_create:{report_public_id}
alert_send:{report_public_id}
```

SQLite 使用 `ON CONFLICT DO NOTHING`，MySQL 使用 `ON DUPLICATE KEY UPDATE`，并由唯一索引保证并发创建时不会出现重复任务。业务表还有第二层幂等约束：一份报告最多对应一个 `LedgerEntry`、`RiskCase` 和 `AlertDelivery`，Excel 文件也会按报告公共 ID 去重。

超过 `max_attempts` 后，任务状态改为 `DEAD`，同时向 `dead_letter_records` 写入任务快照、尝试次数和最后错误。原任务保留，便于管理端查看和后续人工处置。

## 4. Worker 生命周期和并发

FastAPI lifespan 在数据库建表和演示数据初始化后调用 `tool_dispatcher.start()`：

1. 将遗留的 `RUNNING` 任务恢复为 `PENDING`；
2. 启动守护 Dispatcher 线程；
3. 默认每 `1.0` 秒轮询一次到期任务；
4. 使用数据库行锁和 `SKIP LOCKED` 领取任务；
5. 按类型提交到独立的 `ThreadPoolExecutor`；
6. 应用关闭时停止轮询并等待已提交的执行单元结束。

Excel 默认单线程，避免多个 Workbook 写入互相覆盖；个案和预警各有独立线程池，因此慢邮件不会占用 Excel/个案执行槽。失败延迟为：

```text
retry_base_seconds × 2^(attempts - 1)
```

默认最多尝试 3 次。预警执行还有进程内滑动窗口限流，默认每分钟 10 次；被限流的任务重新进入 `PENDING`，且这次延后不消耗失败尝试次数。

当前 Worker 是进程内实现，适合单 FastAPI 进程或封闭部署。若采用多个应用副本，应把“启动时恢复全部 RUNNING”改为基于租约过期恢复，或把 Worker 独立成单实例服务；进程内限流也应改为 Redis 等共享限流器。

## 5. 共用业务操作

`app/tools/operations.py` 是队列 Worker 与 MCP Server 的唯一业务实现：

- `excel_report`：原子替换 `.xlsx` 台账；
- `case_create`：创建风险个案和初始 `CaseEvent`；
- `alert_send`：个案存在后写预警记录，并按配置发送邮件或退化为日志渠道；
- `alert_ack`：幂等确认预警并记录 `AlertEvent`；
- `case_note_add`：追加个案备注、`CaseEvent` 和可用时的管理员审计记录；
- `alert_notify`：对已有预警再次通知并记录事件。

SMTP 仅在 `SMTP_HOST`、`SMTP_FROM_ADDRESS` 和 `ALERT_EMAIL_TO` 都配置后启用；否则预警仍完整落库，但渠道为 `log`。邮件属于外部系统副作用，当前语义是至少一次尝试；邮件服务商不可用时由数据库队列负责重试。

## 6. FastMCP Server

`app/mcp/server.py` 使用官方 Python SDK 的 `FastMCP`，通过 `@mcp.tool()` 暴露：

```text
mindbridge_excel_report
mindbridge_case_create
mindbridge_alert_send
mindbridge_alert_ack
mindbridge_case_note_add
mindbridge_alert_notify
```

启动 stdio Server：

```bash
python -m app.mcp.server
```

示例客户端会启动该 stdio 子进程、创建 `ClientSession`、执行 `initialize()`，再按风险依次调用工具：

```bash
python -m app.mcp.client <report_public_id> --risk high
```

调用顺序为 Excel → 个案 → 预警。MCP 客户端适合外部 Agent 主动编排；聊天主链路仍使用持久化队列，以获得事务边界、重试、限流和死信能力。

## 7. 配置

```text
TOOL_WORKER_POLL_SECONDS=1.0
TOOL_WORKER_BATCH_SIZE=20
TOOL_WORKER_MAX_ATTEMPTS=3
TOOL_WORKER_RETRY_BASE_SECONDS=2.0
TOOL_WORKER_RUNNING_TIMEOUT_SECONDS=300
TOOL_WORKER_EXCEL_WORKERS=1
TOOL_WORKER_CASE_WORKERS=2
TOOL_WORKER_ALERT_WORKERS=2
ALERT_RATE_LIMIT_PER_MINUTE=10

SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM_ADDRESS=
ALERT_EMAIL_TO=
SMTP_USE_TLS=true
```

## 8. 验证

```bash
pytest tests/test_tool_queue_and_mcp.py
pytest
```

专项测试覆盖高风险依赖 DAG、重复入队、执行结果幂等、死信、启动恢复、异步非阻塞、六个 MCP 工具注册，以及真实 stdio MCP 高风险调用链。
