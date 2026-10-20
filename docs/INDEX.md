# MindNook 文档索引

> 文档版本：1.2
> 更新日期：2026-08-24

## 1. 阅读入口

| 文档 | 定位 | 适合场景 |
|---|---|---|
| [PRD](./PRD.md) | 产品需求基线（含目标态标注） | 确认用户、当前业务规则、目标功能和验收标准 |
| [需求分析](./REQUIREMENTS_ANALYSIS.md) | 目标需求评审稿 | 查看需求编号、目标权限/治理能力、实现差距和验收追踪 |
| [技术文档](./TECHNICAL.md) | 当前系统总览 | 快速了解技术栈、架构、数据、接口、部署和测试 |
| [技术实施方案](./IMPLEMENTATION_PLAN.md) | 分阶段实施蓝图 | 查看设计背景、任务拆分、上线门禁和规划决策 |
| [Agent Runtime](./AI_RUNTIME.md) | 多智能体推理实现 | 理解黑板协议、Coordinator、专业 Agent 和协作 Trace |
| [AgentHarness](./AGENT_HARNESS.md) | 聊天业务编排实现 | 理解会话、隐私、持久化、事件交付和 Runtime 边界 |
| [Engineering Harness](./ENGINEERING_HARNESS.md) | 一键工程验证 | 验证 Risk Safety、Agent Routing、Standard Skills、RAG、API 和 Tool Queue |
| [心理场景 Skill 系统](./SKILLS.md) | 可维护的心理表达规则资产 | 理解 Skill 文件规范、路由、状态校验、安全降级和交接摘要 |
| [上下文与分层记忆](./MEMORY.md) | 记忆与 Prompt 上下文实现 | 理解 Redis/MySQL 回填、压缩、私有记忆和审计记忆 |
| [简要记忆升级方案](./MEMORY_UPGRADE_PLAN.md) | 记忆升级设计基线 | 理解最近原文、会话摘要和相关长期记忆的读写与召回 |
| [Memory 测试方案](./MEMORY_TEST_PLAN.md) | 测试前制定的验收矩阵 | 查看隔离环境、边界与故障注入、回归命令 |
| [Memory 测试结果](./MEMORY_TEST_RESULTS.md) | 本次执行记录 | 查看通过数量、发现的问题与未验证范围 |
| [长对话可靠性测试方案](./MEMORY_RELIABILITY_TEST_PLAN.md) | 真实失败回放与后台流程验证 | 查看来源错配、更正误伤、原文回退、迁移测试 |
| [长对话可靠性修复结果](./MEMORY_RELIABILITY_RESULTS.md) | 本轮变更及复测记录 | 查看具体文件、离线测试、真实 API 复测与剩余限制 |
| [RAG 模块](./RAG.md) | 本地混合检索实现 | 理解知识切块、索引、召回、融合、重排和评测 |
| [MCP 与异步工具队列](./MCP_AND_TOOL_QUEUE.md) | 后台副作用实现 | 理解任务 DAG、幂等、重试、限流、死信和 MCP |

## 2. 文档边界

文档按“产品基线 → 系统总览 → 模块实现”分层维护：

- `PRD.md` 描述产品为什么做、面向谁以及怎样验收，并明确当前基线与目标态；涉及已实现行为时仍以代码为准。
- `REQUIREMENTS_ANALYSIS.md` 是目标需求评审稿，其中角色细分、治理和部分管理能力不代表当前已经实现。
- `TECHNICAL.md` 描述当前已实现系统的整体结构，细节以下方模块文档为准。
- `IMPLEMENTATION_PLAN.md` 是历史蓝图与现状对照；其中的目标数据字典、目录结构、阶段计划和上线治理不等同于当前完成状态。
- `AI_RUNTIME.md`、`AGENT_HARNESS.md`、`SKILLS.md`、`MEMORY.md`、`RAG.md`、`MCP_AND_TOOL_QUEUE.md` 分别作为对应模块的权威实现说明。
- 当实施方案与当前实现文档不一致时，以代码、自动化测试和对应模块文档为准，并同步修订总览。

`MEMORY.md` 已合并原“上下文管理模块”文档，避免两份文档重复描述 `memoryBrief`、`modelHistory`、Agent 私有记忆和 Trace。

## 3. 推荐阅读顺序

新成员建议按以下顺序阅读：

1. [PRD](./PRD.md)：先理解业务边界和安全要求；需要追踪目标需求时再看 [需求分析](./REQUIREMENTS_ANALYSIS.md)。
2. [技术文档](./TECHNICAL.md)：建立当前系统全貌。
3. [AgentHarness](./AGENT_HARNESS.md) 与 [Agent Runtime](./AI_RUNTIME.md)：理解一轮聊天怎样编排和推理。
4. 维护心理场景规则时阅读 [心理场景 Skill 系统](./SKILLS.md)。
5. 按任务阅读 [上下文与分层记忆](./MEMORY.md)、[RAG 模块](./RAG.md) 或 [MCP 与异步工具队列](./MCP_AND_TOOL_QUEUE.md)。
6. 需要追溯设计背景或规划后续工作时，再查看 [技术实施方案](./IMPLEMENTATION_PLAN.md)。

## 4. 命名与维护约定

- 文档文件名统一使用大写英文和下划线，例如 `AGENT_HARNESS.md`。
- 标题使用中文业务名称；代码类型、配置项和协议名保留项目中的英文拼写。
- 模块文档头部统一维护“文档版本”“更新日期”和主要实现位置。
- 总览只保留稳定结论，算法、阈值和故障处理放在对应模块文档中，避免多处复制。
- 新增、删除或重命名文档时，必须同步更新本索引和项目根目录 `README.md` 中的链接。
- 文档中的相对链接应在仓库内可解析；代码路径必须与当前目录结构一致。
