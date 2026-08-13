from __future__ import annotations

from pathlib import Path
import shutil

from docx import Document
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph


SOURCE = Path(r"D:\Projects\Agent\MindBridge\项目提交文档")
STAGE = Path(r"D:\Projects\Agent\MindBridge\MindNook\.codex_work\skills_docs_updated")


def insert_paragraph_before(paragraph, text: str, style: str = "Normal"):
    new_p = OxmlElement("w:p")
    paragraph._p.addprevious(new_p)
    new = Paragraph(new_p, paragraph._parent)
    new.style = style
    new.add_run(text)
    return new


def set_paragraph(paragraph, text: str):
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def find_paragraph(doc: Document, exact: str):
    for p in doc.paragraphs:
        if p.text.strip() == exact:
            return p
    raise ValueError(f"paragraph not found: {exact}")


def delete_paragraph(paragraph):
    element = paragraph._element
    element.getparent().remove(element)
    paragraph._p = paragraph._element = None


def insert_section_before(anchor, heading: str, body: list[tuple[str, str]]):
    insert_paragraph_before(anchor, heading, "Heading 1")
    for style, text in body:
        insert_paragraph_before(anchor, text, style)


def replace_token_in_doc(doc: Document, old: str, new: str):
    seen = set()
    paras = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                paras.extend(cell.paragraphs)
    for p in paras:
        if p._p in seen:
            continue
        seen.add(p._p)
        for run in p.runs:
            if old in run.text:
                run.text = run.text.replace(old, new)


def update_main(doc: Document):
    # Part 1: make the new maintainable Skill capability explicit.
    anchor = find_paragraph(doc, "5.5 工具调用与任务执行")
    insert_paragraph_before(anchor, "5.5 心理场景 Skill 管理", "Heading 2")
    skill_requirements = [
        "1．心理场景中的表达规则应以 skills/*/SKILL.md 作为独立资产维护，不与 Agent 调度、风险定级、RAG 事实或业务工具逻辑混写。",
        "2．系统启动时应扫描 Skill 目录，校验目录命名、UTF-8、YAML frontmatter、name、description 和 Workflow；READY、WARN 可加载，FAILED 不得进入 Prompt。",
        "3．日常交流不加载心理 Skill；心理支持固定加载基础支持与转介 Skill，并按焦虑、睡眠、学业主题叠加；风险处置优先加载高风险安全计划与转介 Skill。",
        "4．Skill 不得降低固定安全规则的风险下限，不得编造电话号码、办公时间、人员或政策；可变事实必须来自经审核的 RAG 上下文。",
        "5．管理端应提供 Skill 状态和问题列表；修改文件后需重启应用，新的 Workflow 才进入 Runtime。",
        "6．心理支持和风险处置报告应使用 counselor_handoff_summary 生成工作人员交接摘要；摘要不进入学生端 Prompt，原始表达只保留必要摘录。",
    ]
    for text in skill_requirements:
        insert_paragraph_before(anchor, text, "Normal")
    # Renumber the remaining requirement subsections.
    set_paragraph(anchor, "5.6 工具调用与任务执行")
    set_paragraph(find_paragraph(doc, "5.6 风险识别与安全响应"), "5.7 风险识别与安全响应")
    set_paragraph(find_paragraph(doc, "5.7 管理、审计与运营"), "5.8 管理、审计与运营")

    # Replace obsolete two-skill wording in ContextAgent.
    old = find_paragraph(doc, "知识检索采用 Chroma 向量召回与 BM25 关键词召回的混合方式，经过分数融合、本地重排和相邻上下文扩展后返回相关证据。风险场景加载 crisis_response 支持技能，其他心理支持场景加载 general_support 技能。")
    set_paragraph(old, "知识检索采用 Chroma 向量召回与 BM25 关键词召回的混合方式，经过分数融合、本地重排和相邻上下文扩展后返回相关证据。ContextAgent 另由 SkillRegistry 按等级与主题选择心理场景 Skill：日常交流不加载；心理支持加载基础支持、转介及匹配的焦虑、睡眠或学业 Skill；风险处置加载高风险安全计划与转介 Skill。")

    # Rebuild the unfinished, duplicated back half of part 2 into a concise framework.
    start = find_paragraph(doc, "5 模型、上下文与知识设计")
    end = find_paragraph(doc, "第三部分 测试方案与结果")
    current = start
    while current is not None and current._p is not end._p:
        nxt_xml = current._p.getnext()
        delete_paragraph(current)
        if nxt_xml is None or nxt_xml is end._p:
            break
        # Resolve the next paragraph from the current document XML.
        current = next(p for p in doc.paragraphs if p._p is nxt_xml)

    sections = [
        ("5 模型、上下文与知识设计", [
            ("Heading 2", "5.1 Prompt 与结构化输出"),
            ("Normal", "基础 Prompt 只保留 Agent 角色、输入输出协议和不可绕过的通用安全约束；具体心理场景表达由本轮加载的 Skill Workflow 提供。UnderstandingAgent 与 SafetyAgent 使用结构化结果，ResponseAgent 读取 intent、risk、context 和约束生成候选回复。"),
            ("Heading 2", "5.2 模型配置与调用预算"),
            ("Normal", "模型层通过统一 ModelProvider 支持 Mock、本地模型和 OpenAI-compatible API，并可按 Agent 分别配置。CoordinatorAgent 统一限制协作轮数、模型调用次数、单次超时和总截止时间，超限后采用确定性降级。"),
            ("Heading 2", "5.3 上下文与分层记忆"),
            ("Normal", "MySQL 保存完整消息，Redis 缓存当前会话最近 40 条脱敏消息；未命中时从数据库回填。长历史压缩为最多 500 字的 memoryBrief，最近 8 条保留角色化脱敏原文 modelHistory。各 Agent 私有记忆按独立 Key 隔离，Trace 仅用于审计，不回流后续 Prompt。"),
            ("Heading 2", "5.4 RAG 触发与混合检索"),
            ("Normal", "日常交流不触发心理 RAG；心理支持和风险处置执行 Chroma 向量召回与 BM25 关键词召回，经归一化融合、本地重排和首块相邻上下文扩展后返回最多 3 条证据。风险处置补充当前安全、危机支持与现实求助主题；检索异常时降级使用内置审核知识。"),
        ]),
        ("6 心理场景 Skill 系统", [
            ("Heading 2", "6.1 定位与运行边界"),
            ("Normal", "Skill 是可扫描、可校验、可测试的心理表达规则资产，只规定特定场景中如何表达以及禁止哪些内容，不承担任务调度、风险定级、记忆读取、RAG 检索、数据库访问或工具调用。ContextAgent 负责选择并注入 Workflow，ResponseAgent 据此生成候选回复，SafetyAgent 使用同一 Workflow 完成审查或修复。"),
            ("Heading 2", "6.2 内置 Skill 与路由"),
            ("Normal", "系统内置 7 个 Skill：基础支持、现实转介、焦虑稳定、睡眠安排、学业压力计划、高风险安全计划和工作人员交接摘要。日常交流不加载心理 Skill；心理支持加载基础支持与转介，并可叠加焦虑、睡眠、学业 Skill；风险处置只加载高风险安全计划与转介，避免一般建议分散当前安全重点。"),
            ("Heading 2", "6.3 扫描、校验与安全降级"),
            ("Normal", "SkillRegistry 校验小写下划线目录名、UTF-8、YAML frontmatter、name 与目录一致性、description 和 Workflow。READY、WARN 可注入 Runtime，FAILED 不加载。心理支持缺少基础支持 Skill、风险处置缺少高风险安全计划时，系统分别使用固定安全回复，并继续经过确定性审查。"),
            ("Heading 2", "6.4 工作人员交接摘要"),
            ("Normal", "counselor_handoff_summary 不进入学生端 Prompt。AgentHarness 在心理支持和风险处置报告取得 ID 后，以固定字段生成工作人员可见摘要，包括报告 ID、学生身份、分级结果、情绪标签、置信度、模型摘要、跟进建议和必要原始表达摘录。风险个案复用同一摘要，避免信息漂移；敏感摘要仅在授权人员打开报告上下文时读取并记录审计。"),
            ("Heading 2", "6.5 维护与可观测性"),
            ("Normal", "管理端“系统运行—Skills”和 GET /api/v1/admin/skills 展示状态及 issues。修改 Skill 后管理端扫描结果可立即更新，但 Runtime 使用启动时快照，需重启应用后才加载新 Workflow。新增 Skill 时必须同步确定性路由、必需资产清单与自动化测试。"),
        ]),
        ("7 安全、工具与业务闭环", [
            ("Heading 2", "7.1 分级与回复安全"),
            ("Normal", "日常交流不触发心理 RAG、报告或风险工具；心理支持生成心理报告、交接摘要和台账，不创建个案；风险处置生成报告、交接摘要、台账和唯一风险个案，预警任务依赖个案创建成功。固定安全规则建立不可降低的风险下限，候选回复通过 SafetyAgent 审查后才允许公开。"),
            ("Heading 2", "7.2 PII、权限与服务边界"),
            ("Normal", "手机号、邮箱、证件号、学号等信息在进入模型和短期记忆前脱敏；学生、工作人员和系统运营权限隔离。系统不作临床诊断、不推荐药物、不提供危险细节、不输出后台等级，也不编造未经知识上下文确认的校园资源。"),
            ("Heading 2", "7.3 工具 DAG 与异步执行"),
            ("Normal", "AgentHarness 在最终结果事务中持久化 ToolJob，提交后由 Dispatcher 异步执行。心理支持只创建 ledger_write；风险处置创建 ledger_write、case_create 和依赖 case_create 的 alert_send。任务使用稳定幂等键，支持有限重试、限流、死信和服务重启恢复，失败不撤销学生已经收到的安全回复。"),
        ]),
        ("8 接口、数据与可观测性", [
            ("Heading 2", "8.1 Runtime 与 Harness 接口"),
            ("Normal", "CoordinatorAgent 对外保留 plan()、stream_response() 和 finalize_response() 三阶段接口；AgentHarness 负责认证后的业务编排、落库与公开事件。模型 Token 只形成内部候选流，必须在审查、最终采纳和事务提交后，才由 SSE 输出安全成品。"),
            ("Heading 2", "8.2 核心数据与事件"),
            ("Normal", "主要数据包括消息、PsychologicalReport、handoff_summary、RiskCase、ToolJob、AgentRun、AgentStep、CollaborationEvent 和 Trace。黑板事件 sequence 单调递增，任务、产物、消息与事件只追加不覆盖，可还原每轮分流、检索、生成、审查、修复与采纳过程。"),
            ("Heading 2", "8.3 管理端观测"),
            ("Normal", "授权人员可查看 Agent Run、步骤、协作事件、后台任务、预警以及 Skill 状态；报告交接摘要和原始表达采用按需读取并记录审计。运行数据不直接返回学生端，也不进入下一轮模型上下文。"),
        ]),
        ("9 异常、测试与部署", [
            ("Heading 2", "9.1 异常与降级"),
            ("Normal", "模型失败使用固定模板；Redis 失败从 MySQL 回填；RAG 失败使用内置审核知识；工具失败进入重试与死信；关键 Skill 缺失使用固定安全回复。任何降级都不得降低固定风险下限或绕过回复安全审查。"),
            ("Heading 2", "9.2 测试与验收"),
            ("Normal", "测试覆盖多智能体协作、记忆隔离、RAG 触发与降级、Skill 校验与路由、报告交接摘要、工具 DAG、三级典型场景和权限审计。验收重点确认：日常交流无心理副作用；心理支持生成报告、摘要和台账；风险处置创建个案，且预警严格依赖个案成功。"),
            ("Heading 2", "9.3 部署与当前限制"),
            ("Normal", "部署需配置模型、MySQL、Redis、Chroma、Skill 目录、执行预算和工具 Worker。当前 Runtime 在启动时读取 Skill 快照，修改后需要重启；本地 RAG 的严格质量基线尚未全部达标，真实校园资源、预警接收人和人工处置规范仍需上线前完成专业审核。"),
        ]),
    ]
    for heading, body in sections:
        insert_section_before(end, heading, body)

    # Small style/wording corrections found during the framework audit.
    set_paragraph(find_paragraph(doc, "4.1 CollaborationBlackboard设计"), "4.1 CollaborationBlackboard 设计")
    set_paragraph(find_paragraph(doc, "4.6协作时序图"), "4.6 协作时序图")
    replace_token_in_doc(doc, "风险候选回复回复", "风险候选回复")


def update_outline(doc: Document):
    # Requirement framework.
    anchor = find_paragraph(doc, "5.5 工具调用与任务执行")
    insert_paragraph_before(anchor, "5.5 心理场景 Skill 管理", "Heading 2")
    set_paragraph(anchor, "5.6 工具调用与任务执行")
    set_paragraph(find_paragraph(doc, "5.6 风险识别与安全响应"), "5.7 风险识别与安全响应")
    set_paragraph(find_paragraph(doc, "5.7 管理、审计与运营"), "5.8 管理、审计与运营")

    # Concise part-2 outline matching the new main document framework.
    start = find_paragraph(doc, "4 智能体内部结构")
    end = find_paragraph(doc, "1 测试概述")
    # Keep sections 1-6; replace sections 7-17.
    current = start
    while current is not None and current._p is not end._p:
        nxt_xml = current._p.getnext()
        delete_paragraph(current)
        if nxt_xml is None or nxt_xml is end._p:
            break
        current = next(p for p in doc.paragraphs if p._p is nxt_xml)
    new_outline = [
        ("4 多智能体协作机制", ["4.1 CollaborationBlackboard 设计", "4.2 核心协作对象", "4.3 任务认领与结果发布", "4.4 单轮对话协作流程", "4.5 回复审查与修复流程", "4.6 协作时序图"]),
        ("5 模型、上下文与知识设计", ["5.1 Prompt 与结构化输出", "5.2 模型配置与调用预算", "5.3 上下文与分层记忆", "5.4 RAG 触发与混合检索"]),
        ("6 心理场景 Skill 系统", ["6.1 定位与运行边界", "6.2 内置 Skill 与路由", "6.3 扫描、校验与安全降级", "6.4 工作人员交接摘要", "6.5 维护与可观测性"]),
        ("7 安全、工具与业务闭环", ["7.1 分级与回复安全", "7.2 PII、权限与服务边界", "7.3 工具 DAG 与异步执行"]),
        ("8 接口、数据与可观测性", ["8.1 Runtime 与 Harness 接口", "8.2 核心数据与事件", "8.3 管理端观测"]),
        ("9 异常、测试与部署", ["9.1 异常与降级", "9.2 测试与验收", "9.3 部署与当前限制"]),
    ]
    for h1, children in new_outline:
        insert_paragraph_before(end, h1, "Heading 1")
        for h2 in children:
            insert_paragraph_before(end, h2, "Heading 2")


def update_summary(doc: Document):
    core_anchor = find_paragraph(doc, "后台任务与工具治理：通过持久化任务队列实现依赖编排、幂等、重试、限流和死信处理，并提供 FastMCP 工具接口。")
    insert_paragraph_before(core_anchor, "心理场景 Skill 系统：将基础支持、现实转介、焦虑稳定、睡眠安排、学业压力、高风险安全计划和工作人员交接摘要沉淀为 7 个可扫描、可校验的 Workflow；ContextAgent 按等级与主题加载，关键 Skill 失效时使用固定安全回复。", "Normal")
    tech = find_paragraph(doc, "项目采用 Python 与 FastAPI 构建后端服务，以 SQLAlchemy 管理业务数据，兼容 MySQL 和 SQLite；Redis 承载短期会话及 Agent 私有记忆，Chroma 保存本地向量索引。AgentHarness 负责认证后的业务编排、脱敏、持久化、报告和事件交付，CoordinatorAgent 基于 CollaborationBlackboard 驱动专业 Agent 协作。模型层支持 Mock、本地模型和 OpenAI-compatible API；检索层采用本地特征向量与 BM25 混合召回。安全上以固定安全规则设定不可降低的风险下限，并在候选回复公开前完成安全审查。外部副作用在数据库事务提交后由异步 Worker 执行，保证聊天结果稳定且操作可追踪。")
    set_paragraph(tech, tech.text.replace("CoordinatorAgent 基于 CollaborationBlackboard 驱动专业 Agent 协作。", "CoordinatorAgent 基于 CollaborationBlackboard 驱动专业 Agent 协作，SkillRegistry 扫描并路由心理场景 Workflow。"))
    innovation = find_paragraph(doc, "一是将固定安全规则与模型判断结合，采用“规则兜底、模型增强、发送前复审”的多层防线，避免模型将明确风险错误降级。二是以追加式协作黑板保存任务、产物、消息和事件，使多智能体决策不再是不可解释的串行调用。三是把业务事实、短期上下文、Agent 私有记忆和审计 Trace 分层隔离，既保持对话连续性，又减少隐私泄露和角色信息污染。四是将本地混合检索、确定性上下文压缩与可降级设计结合，在外部模型或基础设施异常时仍保留基本支持能力。")
    set_paragraph(innovation, innovation.text + "五是把心理场景话术从基础 Prompt 中拆分为可维护、可测试的 Skill 资产，使表达规则、知识事实、风险判断和业务调度各自保持清晰边界。")


def main():
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    for path in sorted(SOURCE.glob("*.docx")):
        doc = Document(path)
        if path.name.endswith("设计方案书.docx"):
            update_main(doc)
        elif "大纲" in path.name:
            update_outline(doc)
        elif "项目概要介绍" in path.name:
            update_summary(doc)
        out = STAGE / path.name
        doc.save(out)
        print(path.name, out.stat().st_size, len(doc.paragraphs), len(doc.tables))


if __name__ == "__main__":
    main()
