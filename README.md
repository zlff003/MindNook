# MindNook

MindNook 是一个校园心理健康问答系统的最小可运行闭环，包含学生端和管理端。

项目文档的职责边界、推荐阅读顺序和维护约定见 [文档索引](docs/INDEX.md)。

## 本地运行

MySQL 和 Redis 使用 Docker 启动，Python 后端直接在本机运行：

```bash
docker compose up -d mysql redis
python -m pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8080
```

MySQL 默认映射到宿主机 `3307`（容器内仍为 `3306`），以避免与本机已有的 MySQL 冲突。需要更换时可在 `.env` 中修改 `MYSQL_HOST_PORT`，并同步调整 `DATABASE_URL`。

macOS/Linux 复制配置使用：

```bash
cp .env.example .env
```

检查基础设施：

```bash
docker compose ps
docker compose exec redis redis-cli ping
docker compose exec mysql mysqladmin ping -h localhost -uroot -proot-local
```

停止基础设施但保留数据卷：

```bash
docker compose down
```

后端不构建、不运行在 Docker 中，仓库不再提供后端 Dockerfile。

也可以在不启动 MySQL 时使用默认 SQLite 配置进行纯本地开发：

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

访问：

- 学生端：<http://127.0.0.1:8080/student>
- 管理端：<http://127.0.0.1:8080/admin>

本地演示账号：

- 学生：`student` / `student123`
- 管理员：`admin` / `admin123`

演示账号和默认密钥仅用于本地开发，部署前必须更换。

## 测试

```bash
python -m unittest discover -s tests
```

也可以运行：

```bash
pytest
```

运行完整 Agent 链路工程验证：

```bash
python -m app.harness.runner
```

该命令以临时 SQLite、内存短期记忆和 Mock 模型执行六类工程验证；使用
`--json` 输出机器可读报告，使用 `--suite <name>` 选择单个套件。

Harness 的职责边界与聊天编排见 [AgentHarness 工程链路说明](docs/AGENT_HARNESS.md)，一键验证套件见 [Engineering Harness](docs/ENGINEERING_HARNESS.md)。分层记忆、上下文压缩、Prompt 组装和审计记忆设计见 [上下文与分层记忆模块](docs/MEMORY.md)。

异步工具 Worker 的任务 DAG、幂等、重试、限流、死信以及 FastMCP stdio 用法见 [MCP 与异步工具队列](docs/MCP_AND_TOOL_QUEUE.md)。

启动 MCP Server，或让示例客户端按风险执行报告工具链：

```bash
python -m app.mcp.server
python -m app.mcp.client <report_public_id> --risk RISK
```

## 模型切换

默认使用无需网络的 Mock Provider。复制 `.env.example` 为 `.env`，配置以下变量后可切换阿里百炼 OpenAI-compatible API：

```text
MODEL_PROVIDER=bailian
MODEL_API_KEY=your-key
MODEL_NAME=qwen-plus
```

聊天主链路按以下分层执行：

```text
HTTP（认证 + SSE）
  -> ChatService（只转发公开事件）
  -> AgentHarness（脱敏、会话、落库、报告、工具计划、Trace）
  -> Agent Runtime（纯多 Agent 推理）
```

Agent Runtime 使用 CollaborationBlackboard 协作。Agent 不互相调用方法，而是 claim task、发布 artifact/message/event，再由黑板合并 AgentTurnResult：

```text
CoordinatorAgent 创建 root task
  -> UnderstandingAgent 发布 intent
  -> SafetyAgent 发布 risk / SAFETY_OVERRIDE
  -> ContextAgent 发布 memory / RAG / Skill context
  -> ResponseAgent 发布 response_proposal
  -> SafetyAgent 发布 safety_review
  -> CoordinatorAgent 发布 FINAL_ADOPTED
```

所有 Task、Artifact、Message 和 Event 都采用追加式记录；候选拒绝后的修复会保留 v1/v2，不覆盖历史。固定规则给出的高风险下限不能被模型降级；整个运行受最大协作轮数、模型调用次数和总时限限制。

## 混合 RAG

心理支持和安全场景默认由 ContextAgent 触发本地混合检索：

```text
中文 Markdown
  -> 默认按 512 字符切块、相邻块重叠 64 字符
  -> 本地中文特征 Embedding 写入 Chroma 并执行向量召回
  -> BM25 关键词召回
  -> 向量分与 BM25 分分别归一化，默认按 0.45 / 0.55 融合
  -> 使用 query 覆盖率、短语命中和词面相似度本地重排
  -> 为第一名补充同文档前后块
```

知识文件位于 `data/knowledge/*.md`，Chroma 持久化数据默认位于 `data/chroma/`。Embedding、BM25 和重排均在本地执行，不需要运行时下载额外模型。RAG 不可用时会降级到少量内置知识，不能阻断基础对话和安全回复。

完整的索引生命周期、检索公式、ContextAgent 接入、评测和维护方式见 [RAG 模块](docs/RAG.md)。

## 心理场景 Skills

心理回复规则位于 `skills/*/SKILL.md`。每个 Skill 必须包含仅有 `name`、`description` 的 YAML frontmatter，以及 `## Workflow`。项目约定 Skill 名和目录名使用小写下划线并保持一致。

Runtime 启动时扫描并校验 Skill；ContextAgent 根据风险等级和主题加载 Workflow，ResponseAgent 生成候选回复，SafetyAgent 按同一 Workflow 审查和修复。管理员可在“系统运行 → Skills”查看 `READY`、`WARN`、`FAILED` 状态。Skill 只提供场景表达规则，不负责任务调度、RAG 检索或数据库访问。

七个内置 Skill、三级风险路由、校验状态、安全降级、交接摘要和维护测试方式见 [心理场景 Skill 系统](docs/SKILLS.md)。

## RAG 评测

评测集位于 `data/evaluation/RAG评测集.jsonl`，包含 85 条中文检索样例、15 条不应触发 RAG 的普通问答，其中 10 条为安全关键样例，另有 9 条跨 Chunk 证据标注。离线评测同时比较仅向量、仅 BM25、融合、重排、最终扩展五个阶段，并检查 Hit@1、Recall@3、MRR@3、nDCG@3、Top1 证据召回、阶段胜平负、挑战类型分层、路由误触发率、安全召回率和上下文重复率。

生成 JSON 与中文 Markdown 报告：

```bash
python -m app.rag.evaluation
```

用于 CI，任一验收阈值不达标时返回非零状态：

```bash
python -m app.rag.evaluation --strict
```

需要额外运行 Mock 回复质量回归时：

```bash
python -m app.rag.evaluation --evaluate-answers
```

自动词面指标只能发现回归，真实模型上线前还需按 `data/evaluation/回答质量人工评审表.md` 进行人工盲审，特别检查事实支撑、资源真实性、隐私和危机响应。

## 当前闭环

- 两种账号角色与服务端权限隔离。
- 学生多会话与 SSE 回复。
- 普通交流、心理支持和安全风险分类。
- 固定高风险规则与安全回复。
- PII 脱敏后进入智能体运行层。
- Redis 会话短期记忆：`mindbridge:short-term-memory:<session_id>`，最多 40 条、TTL 1 天。
- Redis 未命中时从 `chat_messages` 最近 40 条回填。
- 长历史按“最近 4 条学生关注、3 条助手支持和本轮输入”确定性压缩为最多 500 字的 `memoryBrief`；Prompt 保留最近 8 条带角色脱敏原文 `modelHistory`。
- Understanding、Safety、Context、Response 使用互相隔离的 Agent 私有 Redis 记忆。
- ContextAgent 默认使用 Chroma 向量召回、BM25、归一化融合、本地重排和首块前后文扩展。
- `agent_run_traces` 保存输入、脱敏输入、记忆、检索、评估和黑板协作审计记录。
- 心理报告、工作台账、风险个案，以及可配置 SMTP 的邮件预警（未配置时使用日志渠道）。
- 后台工具真实写入 `.xlsx` 派生台账，并采用报告 ID 去重和原子替换。
- `tool_jobs` 持久化后台任务、依赖 DAG、幂等创建、指数重试、进程内限流和 `dead_letter_records` 死信记录。
- 官方 FastMCP stdio Server 六个工具，并与队列共用 Excel、个案和预警业务层。
- 管理端概览、报告、必要上下文、个案、预警和任务。
- 管理端可查看 Agent Step 与完整 CollaborationBlackboard 事件序列。
- 不依赖 HTTP 的 AgentHarness 验收、Runtime 单测与 API 集成测试。

本版本用于本地开发和封闭演示，不应直接用于真实危机干预或生产环境。
