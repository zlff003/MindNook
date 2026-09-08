# 工程测试工具

> 文档版本：1.1
> 最后验证：2026-08-24

MindNook 提供了一键式工程验证测试工具：

```bash
python -m app.harness.runner
```

该命令会为每个测试套件创建一个全新的临时 SQLite 数据库、一个内存中的 Redis 兼容短期记忆存储、一个确定性的模拟模型提供者，以及一个临时的 RAG 索引。它永远不会读取或写入配置的生产数据库、Redis 实例或风险分类账。

当前隔离注意事项：`HarnessEnvironment` 不会显式清除 `Settings` 从进程环境或 `.env` 继承的 SMTP 设置。如果配置了 `SMTP_HOST`、`SMTP_FROM_ADDRESS` 和 `ALERT_EMAIL_TO`，则 `tool-queue` 测试套件可能会尝试真实的 SMTP 连接。在将此命令视为无副作用之前，请清除这些设置或修复测试工具的隔离机制。

## 测试套件

| 测试套件          | 核心验证内容                                                 |
| ----------------- | ------------------------------------------------------------ |
| `risk-safety`     | 明确的高风险检测、心理报告持久化、后端元数据隔离、持久化工具任务入队 |
| `agent-routing`   | `CHAT` / `CONSULT` / `RISK` 路由、持久化 Agent 步骤、协作参与者 |
| `standard-skills` | 必需技能验证、CONSULT/RISK 选择、工作流注入                  |
| `rag`             | 108 个案例数据集覆盖，以及 Hit@1、Recall@3、MRR@3、nDCG@3、安全召回、重复率指标 |
| `api`             | 存活/就绪检查、登录、授权、公共 SSE、管理员知识状态/来源/重建 API |
| `tool-queue`      | 案例/告警 DAG、幂等性、依赖关系、执行、限流、死信队列        |

通过重复 `--suite` 参数运行选定的测试套件：

```bash
python -m app.harness.runner --suite risk-safety --suite tool-queue
```

生成完整的机器可读报告：

```bash
python -m app.harness.runner --json
```

仅当所有选定的测试套件和检查均通过时，进程才会以退出码 `0` 退出。

## RAG 阈值

一键式测试工具使用确定性的工程回归基准，以便代码或语料库回归能够快速且可重复地失败。其 JSON 输出还包含 `strict_acceptance_passed` 以及独立 RAG 评估中的所有严格检查。更严格的发布质量门禁仍然有效：

```bash
python -m app.rag.evaluation --evaluate-answers --strict
```

通过工程基准测试并不意味着更严格的 RAG 发布门禁或人工答案质量审查已通过。
