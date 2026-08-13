from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agents.runtime import CoordinatorAgent
from app.core.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.harness.agent_harness import AgentHarness, ChatCommand
from app.main import create_app
from app.memory.private import AgentPrivateMemory
from app.memory.service import ShortTermMemoryService
from app.memory.store import InMemoryRedisStore
from app.memory.trace import AgentTraceService
from app.models import (
    AgentCollaborationEvent,
    AgentRun,
    AgentStep,
    AlertDelivery,
    Conversation,
    DeadLetterRecord,
    LedgerEntry,
    Message,
    PsychologicalReport,
    RiskCase,
    ToolJob,
    User,
)
from app.rag.evaluation import DEFAULT_DATASET, build_report, load_cases
from app.rag.service import HybridRAGService
from app.services.chat import ChatService
from app.services.jobs import DEAD, SUCCESS, enqueue_report_jobs, enqueue_tool_job
from app.services.model import MockModelProvider
from app.services.tool_dispatcher import InlineToolDispatcher, _PerMinuteRateLimiter
from app.skills.registry import REQUIRED_SKILLS, SkillStatus


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_NAMES = (
    "risk-safety",
    "agent-routing",
    "standard-skills",
    "rag",
    "api",
    "tool-queue",
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    metrics: dict = field(default_factory=dict)


@dataclass
class SuiteResult:
    name: str
    passed: bool
    duration_ms: int
    checks: list[CheckResult]


class RecordingDispatcher:
    """Leaves durable jobs pending so enqueue behavior can be inspected."""

    def __init__(self):
        self.dispatch_count = 0

    def start(self) -> None:
        return None

    def dispatch(self) -> None:
        self.dispatch_count += 1

    def close(self) -> None:
        return None


class HarnessEnvironment:
    """Isolated, deterministic runtime shared by one engineering suite."""

    def __init__(self, name: str):
        self.name = name
        self._temporary = tempfile.TemporaryDirectory(prefix=f"mindnook-{name}-")
        self.root = Path(self._temporary.name)
        self.settings = Settings(
            app_env="test",
            app_secret="engineering-harness-secret",
            database_url=f"sqlite:///{(self.root / 'harness.db').as_posix()}",
            redis_url="memory://engineering-harness",
            model_provider="mock",
            model_name="engineering-harness-model",
            rag_knowledge_dir=str(PROJECT_ROOT / "data" / "knowledge"),
            rag_chroma_path=str(self.root / "chroma"),
            rag_collection_name=f"mindnook_{name.replace('-', '_')}",
            rag_embedding_dimension=128,
            skills_dir=str(PROJECT_ROOT / "skills"),
            excel_ledger_path=str(self.root / "risk-ledger.xlsx"),
            tool_worker_retry_base_seconds=0.01,
            alert_rate_limit_per_minute=1,
        )
        self.engine = build_engine(self.settings.database_url)
        Base.metadata.create_all(self.engine)
        self.session_factory = build_session_factory(self.engine)
        self.redis_store = InMemoryRedisStore()
        self.provider = MockModelProvider()
        self.rag_service = HybridRAGService(self.settings)
        self.runtime = CoordinatorAgent(
            self.provider,
            self.settings,
            rag_service=self.rag_service,
        )
        self.dispatcher = RecordingDispatcher()
        self.harness = AgentHarness(
            self.session_factory,
            self.runtime,
            self.dispatcher,
            ShortTermMemoryService(self.settings, self.redis_store),
            AgentPrivateMemory(self.settings, self.redis_store),
            AgentTraceService(),
        )

    def close(self) -> None:
        self.dispatcher.close()
        self.redis_store.close()
        self.engine.dispose()
        self._temporary.cleanup()

    def create_student_conversation(self, suffix: str) -> tuple[str, str]:
        db = self.session_factory()
        try:
            student = User(
                username=f"harness-{self.name}-{suffix}",
                display_name="Engineering Harness Student",
                password_hash="not-used",
                role="student",
                status="active",
            )
            db.add(student)
            db.flush()
            conversation = Conversation(student_id=student.id)
            db.add(conversation)
            db.commit()
            return student.public_id, conversation.public_id
        finally:
            db.close()

    def create_report(self, risk_level: str = "RISK") -> PsychologicalReport:
        student_id, conversation_id = self.create_student_conversation(
            f"queue-{time.time_ns()}"
        )
        db = self.session_factory()
        try:
            student = db.scalar(select(User).where(User.public_id == student_id))
            conversation = db.scalar(
                select(Conversation).where(Conversation.public_id == conversation_id)
            )
            source = Message(
                conversation_id=conversation.id,
                sender_type="student",
                content="I need help",
                client_request_id=f"queue-{time.time_ns()}",
            )
            db.add(source)
            db.flush()
            report = PsychologicalReport(
                conversation_id=conversation.id,
                source_message_id=source.id,
                student_id=student.id,
                emotion_summary="distressed",
                risk_level=risk_level,
                confidence=0.99,
                reason_summary="engineering harness risk report",
            )
            db.add(report)
            db.commit()
            db.refresh(report)
            db.expunge(report)
            return report
        finally:
            db.close()


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _check(name: str, condition: bool, detail: str, **metrics) -> CheckResult:
    return CheckResult(name, bool(condition), detail, metrics)


def _run_risk_safety() -> list[CheckResult]:
    env = HarnessEnvironment("risk-safety")
    try:
        student_id, conversation_id = env.create_student_conversation("risk")
        command = ChatCommand(
            student_public_id=student_id,
            conversation_public_id=conversation_id,
            content="我想自杀，现在完全撑不住了。",
            client_request_id="risk-safety-0001",
        )
        all_events = list(env.harness.run(command))
        public_events = list(ChatService(env.harness).stream(command))
        public_types = [event.type for event in public_events]
        public_text = "".join(
            event.payload.get("text", "") for event in public_events if event.type == "delta"
        )
        trace = next(event.payload for event in all_events if event.type == "trace")
        db = env.session_factory()
        try:
            report = db.scalar(select(PsychologicalReport))
            jobs = list(db.scalars(select(ToolJob).order_by(ToolJob.id)))
            checks = [
                _check(
                    "high-risk-detection",
                    trace["risk_level"] == "RISK",
                    "Explicit self-harm language must resolve to RISK.",
                    risk_level=trace["risk_level"],
                ),
                _check(
                    "psychological-report",
                    report is not None and report.risk_level == "RISK",
                    "A RISK turn must persist one psychological report.",
                    reports=_count(db, PsychologicalReport),
                ),
                _check(
                    "backend-metadata-hidden",
                    set(public_types) <= {"accepted", "status", "delta", "completed", "error"}
                    and "risk_level" not in public_text
                    and "tool_plan" not in public_text,
                    "Student-facing events must not expose runtime plans, traces, or risk metadata.",
                    public_events=public_types,
                ),
                _check(
                    "risk-tool-jobs-enqueued",
                    [job.job_type for job in jobs]
                    == ["EXCEL_REPORT", "CASE_CREATE", "ALERT_SEND"]
                    and all(job.status == "PENDING" for job in jobs),
                    "RISK must enqueue the durable Excel/case/alert DAG before workers run.",
                    jobs=[{"type": job.job_type, "status": job.status} for job in jobs],
                    dispatch_count=env.dispatcher.dispatch_count,
                ),
            ]
            return checks
        finally:
            db.close()
    finally:
        env.close()


def _run_agent_routing() -> list[CheckResult]:
    env = HarnessEnvironment("agent-routing")
    try:
        scenarios = (
            ("chat", "请解释 Python 列表推导式。", "CHAT"),
            ("consult", "最近考试压力很大，晚上总是睡不着。", "CONSULT"),
            ("risk", "我想自杀，现在完全撑不住了。", "RISK"),
        )
        observed: dict[str, dict] = {}
        for suffix, message, expected in scenarios:
            student_id, conversation_id = env.create_student_conversation(suffix)
            events = list(
                env.harness.run(
                    ChatCommand(
                        student_public_id=student_id,
                        conversation_public_id=conversation_id,
                        content=message,
                        client_request_id=f"routing-{suffix}-0001",
                    )
                )
            )
            trace = next(event.payload for event in events if event.type == "trace")
            db = env.session_factory()
            try:
                run = db.scalar(
                    select(AgentRun).where(AgentRun.public_id == trace["run_id"])
                )
                steps = list(
                    db.scalars(
                        select(AgentStep)
                        .where(AgentStep.run_id == run.id)
                        .order_by(AgentStep.sequence)
                    )
                )
                actors = set(
                    db.scalars(
                        select(AgentCollaborationEvent.actor).where(
                            AgentCollaborationEvent.run_id == run.id
                        )
                    )
                )
                observed[suffix] = {
                    "expected": expected,
                    "actual": run.risk_level,
                    "steps": [step.step_name for step in steps],
                    "actors": sorted(actors),
                }
            finally:
                db.close()
        required_steps = {"hard_safety_rules", "context_loading", "response_prompt", "response_review"}
        required_agents = {
            "CoordinatorAgent",
            "UnderstandingAgent",
            "SafetyAgent",
            "ContextAgent",
            "ResponseAgent",
        }
        return [
            _check(
                "chat-consult-risk-routing",
                all(item["actual"] == item["expected"] for item in observed.values()),
                "The three canonical messages must route to CHAT / CONSULT / RISK.",
                routes={name: item["actual"] for name, item in observed.items()},
            ),
            _check(
                "agent-step-persistence",
                all(required_steps <= set(item["steps"]) for item in observed.values()),
                "Every route must persist the core planning, context, generation, and review steps.",
                steps={name: item["steps"] for name, item in observed.items()},
            ),
            _check(
                "agent-collaboration",
                all(required_agents <= set(item["actors"]) for item in observed.values()),
                "Every route must leave collaboration records for the coordinator and specialist agents.",
                actors={name: item["actors"] for name, item in observed.items()},
            ),
        ]
    finally:
        env.close()


def _run_standard_skills() -> list[CheckResult]:
    env = HarnessEnvironment("standard-skills")
    try:
        registry = env.runtime.skill_registry
        scanned = {item.directory_name: item for item in registry.skills}
        consult = registry.select_response_skills(
            risk_level="CONSULT",
            message="最近焦虑、失眠，考试压力很大。",
            topics=("anxiety", "失眠", "考试"),
        )
        risk = registry.select_response_skills(
            risk_level="RISK",
            message="我现在不安全。",
        )
        consult_names = {item.directory_name for item in consult}
        risk_names = {item.directory_name for item in risk}
        plan = env.runtime.plan("最近考试压力很大，晚上睡不着。", [])
        return [
            _check(
                "required-skills-ready",
                set(REQUIRED_SKILLS) <= set(scanned)
                and all(scanned[name].status == SkillStatus.READY for name in REQUIRED_SKILLS),
                "All standard project skills must exist and validate as READY.",
                statuses={name: scanned[name].status.value for name in REQUIRED_SKILLS},
            ),
            _check(
                "consult-skill-routing",
                {
                    "supportive_response_baseline",
                    "referral_resource_guidance",
                    "anxiety_grounding_support",
                    "sleep_routine_support",
                    "academic_stress_planning",
                }
                <= consult_names,
                "CONSULT topics must select the standard baseline and topic skills.",
                selected=sorted(consult_names),
            ),
            _check(
                "risk-skill-routing",
                risk_names == {"referral_resource_guidance", "high_risk_safety_plan"},
                "RISK must select referral guidance and the high-risk safety plan.",
                selected=sorted(risk_names),
            ),
            _check(
                "workflow-injected",
                bool(plan.context.skills)
                and len(plan.context.skills) == len(plan.context.skill_context)
                and all(value.startswith("Skill: ") for value in plan.context.skill_context),
                "Selected Skill workflows must be injected into the response context.",
                skills=plan.context.skills,
            ),
        ]
    finally:
        env.close()


def _run_rag() -> list[CheckResult]:
    env = HarnessEnvironment("rag")
    try:
        cases = load_cases(DEFAULT_DATASET)
        report = build_report(env.rag_service, env.settings, cases)
        final = report["retrieval"]["stages"]["final"]
        routing = report["routing"]
        acceptance = report["acceptance"]
        engineering_floor = {
            "hit_at_1": 0.65,
            "recall_at_3": 0.75,
            "mrr_at_3": 0.75,
            "safety_hit_at_1": 0.75,
            "safety_recall_at_3": 0.75,
            "context_duplicate_rate": 0.15,
        }
        retrieval = report["retrieval"]
        floor_passed = (
            final["hit_at_1"] >= engineering_floor["hit_at_1"]
            and final["recall_at_3"] >= engineering_floor["recall_at_3"]
            and final["mrr"] >= engineering_floor["mrr_at_3"]
            and retrieval["safety_hit_at_1"]
            >= engineering_floor["safety_hit_at_1"]
            and retrieval["safety_recall_at_3"]
            >= engineering_floor["safety_recall_at_3"]
            and retrieval["context_duplicate_rate"]
            <= engineering_floor["context_duplicate_rate"]
        )
        return [
            _check(
                "evaluation-dataset",
                report["dataset"]["retrieval_sample_count"] >= 80
                and report["dataset"]["negative_sample_count"] >= 10,
                "RAG evaluation must include broad positive and negative coverage.",
                **report["dataset"],
            ),
            _check(
                "retrieval-metrics",
                floor_passed,
                "Recall metrics must stay above the deterministic engineering regression floor.",
                hit_at_1=final["hit_at_1"],
                recall_at_3=final["recall_at_3"],
                mrr_at_3=final["mrr"],
                ndcg_at_3=final["ndcg_at_3"],
                safety_hit_at_1=retrieval["safety_hit_at_1"],
                safety_recall_at_3=retrieval["safety_recall_at_3"],
                context_duplicate_rate=retrieval["context_duplicate_rate"],
                routing_recall=routing["recall"],
                routing_false_positive_rate=routing["false_positive_rate"],
                engineering_floor=engineering_floor,
                strict_acceptance_passed=acceptance["passed"],
                strict_checks=acceptance["checks"],
            ),
        ]
    finally:
        env.close()


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    if response.status_code != 200:
        raise RuntimeError(f"login failed for {username}: {response.status_code}")
    return response.json()["access_token"]


def _run_api() -> list[CheckResult]:
    with tempfile.TemporaryDirectory(prefix="mindnook-api-harness-") as directory:
        root = Path(directory)
        settings = Settings(
            app_env="test",
            app_secret="engineering-api-harness-secret",
            database_url=f"sqlite:///{(root / 'api.db').as_posix()}",
            redis_url="memory://api-harness",
            model_provider="mock",
            model_name="engineering-harness-model",
            rag_knowledge_dir=str(PROJECT_ROOT / "data" / "knowledge"),
            rag_chroma_path=str(root / "chroma"),
            rag_collection_name="mindnook_api_harness",
            rag_embedding_dimension=128,
            skills_dir=str(PROJECT_ROOT / "skills"),
            excel_ledger_path=str(root / "risk-ledger.xlsx"),
            demo_student_username="student",
            demo_student_password="student123",
            demo_admin_username="admin",
            demo_admin_password="admin123",
        )
        with TestClient(create_app(settings)) as client:
            live = client.get("/health/live")
            ready = client.get("/health/ready")
            student_token = _login(client, "student", "student123")
            admin_token = _login(client, "admin", "admin123")
            student_headers = {"Authorization": f"Bearer {student_token}"}
            admin_headers = {"Authorization": f"Bearer {admin_token}"}
            me = client.get("/api/v1/me", headers=student_headers)
            forbidden_admin = client.get(
                "/api/v1/admin/knowledge/status", headers=student_headers
            )
            forbidden_student = client.post(
                "/api/v1/conversations", headers=admin_headers
            )
            conversation = client.post(
                "/api/v1/conversations", headers=student_headers
            )
            conversation_id = conversation.json()["id"]
            stream = client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                headers=student_headers,
                json={
                    "content": "请解释 Python 循环。",
                    "client_request_id": "api-harness-sse-0001",
                },
            )
            knowledge = client.get(
                "/api/v1/admin/knowledge/status", headers=admin_headers
            )
            sources = client.get(
                "/api/v1/admin/knowledge/sources", headers=admin_headers
            )
            rebuild = client.post(
                "/api/v1/admin/knowledge/index/rebuild", headers=admin_headers
            )
            knowledge_body = knowledge.json() if knowledge.status_code == 200 else {}
            return [
                _check(
                    "health",
                    live.status_code == 200
                    and ready.status_code == 200
                    and ready.json().get("agent_harness") == "ready",
                    "Liveness and readiness endpoints must report the engineering chain ready.",
                    live=live.status_code,
                    ready=ready.status_code,
                ),
                _check(
                    "authentication-authorization",
                    me.status_code == 200
                    and me.json().get("role") == "student"
                    and forbidden_admin.status_code == 403
                    and forbidden_student.status_code == 403,
                    "Student/admin authentication and role boundaries must be enforced.",
                    student_me=me.status_code,
                    student_to_admin=forbidden_admin.status_code,
                    admin_to_student=forbidden_student.status_code,
                ),
                _check(
                    "sse",
                    stream.status_code == 200
                    and stream.headers.get("content-type", "").startswith("text/event-stream")
                    and all(
                        marker in stream.text
                        for marker in ("event: accepted", "event: delta", "event: completed")
                    )
                    and "event: runtime_plan" not in stream.text
                    and "event: trace" not in stream.text,
                    "The chat endpoint must stream only public SSE events.",
                    status=stream.status_code,
                    content_type=stream.headers.get("content-type"),
                ),
                _check(
                    "admin-knowledge-api",
                    knowledge.status_code == 200
                    and sources.status_code == 200
                    and rebuild.status_code == 200
                    and knowledge_body.get("source_count", 0) > 0
                    and knowledge_body.get("chunk_count", 0) > 0,
                    "Admin knowledge status, source listing, and rebuild endpoints must be operational.",
                    status=knowledge.status_code,
                    sources=sources.status_code,
                    rebuild=rebuild.status_code,
                    source_count=knowledge_body.get("source_count", 0),
                    chunk_count=knowledge_body.get("chunk_count", 0),
                ),
            ]


def _run_tool_queue() -> list[CheckResult]:
    env = HarnessEnvironment("tool-queue")
    try:
        report = env.create_report()
        db = env.session_factory()
        try:
            first = enqueue_report_jobs(
                db,
                report,
                ["ledger_write", "case_create", "alert_send"],
            )
            second = enqueue_report_jobs(
                db,
                report,
                ["ledger_write", "case_create", "alert_send"],
            )
            db.commit()
            ids_match = [job.id for job in first] == [job.id for job in second]
            dependency_ok = first[2].depends_on_job_id == first[1].id
            job_count = _count(db, ToolJob)
        finally:
            db.close()

        dispatcher = InlineToolDispatcher(env.session_factory, env.settings)
        dispatcher.start()
        dispatcher.dispatch()
        db = env.session_factory()
        try:
            successful_jobs = list(db.scalars(select(ToolJob).order_by(ToolJob.id)))
            execution_ok = (
                all(job.status == SUCCESS for job in successful_jobs)
                and _count(db, LedgerEntry) == 1
                and _count(db, RiskCase) == 1
                and _count(db, AlertDelivery) == 1
                and Path(env.settings.excel_ledger_path).is_file()
            )
        finally:
            db.close()

        db = env.session_factory()
        try:
            enqueue_tool_job(
                db,
                job_type="UNSUPPORTED_ENGINEERING_JOB",
                idempotency_key="engineering:dead-letter",
                payload={},
                max_attempts=1,
            )
            db.commit()
        finally:
            db.close()
        dispatcher.dispatch()
        db = env.session_factory()
        try:
            dead_job = db.scalar(
                select(ToolJob).where(
                    ToolJob.idempotency_key == "engineering:dead-letter"
                )
            )
            dead_letters = _count(db, DeadLetterRecord)
        finally:
            db.close()
        limiter = _PerMinuteRateLimiter(1)
        first_delay = limiter.acquire_or_delay()
        second_delay = limiter.acquire_or_delay()
        dispatcher.close()
        return [
            _check(
                "durable-dag-and-idempotency",
                ids_match and dependency_ok and job_count == 3,
                "Excel/case/alert jobs must form one idempotent durable DAG.",
                job_count=job_count,
                alert_depends_on_case=dependency_ok,
            ),
            _check(
                "excel-case-alert-execution",
                execution_ok,
                "The queue must execute Excel, case, and alert operations through production handlers.",
                excel_exists=Path(env.settings.excel_ledger_path).is_file(),
            ),
            _check(
                "alert-rate-limit",
                first_delay == 0 and second_delay > 59,
                "The configured per-minute alert limit must defer excess work.",
                first_delay=first_delay,
                second_delay=second_delay,
            ),
            _check(
                "dead-letter",
                dead_job is not None and dead_job.status == DEAD and dead_letters == 1,
                "A permanent tool failure must enter DEAD exactly once.",
                status=dead_job.status if dead_job else None,
                dead_letters=dead_letters,
            ),
        ]
    finally:
        env.close()


SUITES: dict[str, Callable[[], list[CheckResult]]] = {
    "risk-safety": _run_risk_safety,
    "agent-routing": _run_agent_routing,
    "standard-skills": _run_standard_skills,
    "rag": _run_rag,
    "api": _run_api,
    "tool-queue": _run_tool_queue,
}


class EngineeringHarness:
    def run(self, selected: list[str] | tuple[str, ...] | None = None) -> dict:
        selected_names = list(selected or SUITE_NAMES)
        unknown = sorted(set(selected_names) - set(SUITE_NAMES))
        if unknown:
            raise ValueError(f"unknown harness suites: {', '.join(unknown)}")
        started = time.perf_counter()
        suites: list[SuiteResult] = []
        for name in selected_names:
            suite_started = time.perf_counter()
            try:
                checks = SUITES[name]()
            except Exception as exc:  # noqa: BLE001 - report suite failure, keep running.
                checks = [
                    CheckResult(
                        name="suite-execution",
                        passed=False,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                ]
            suites.append(
                SuiteResult(
                    name=name,
                    passed=all(check.passed for check in checks),
                    duration_ms=round((time.perf_counter() - suite_started) * 1000),
                    checks=checks,
                )
            )
        check_count = sum(len(suite.checks) for suite in suites)
        passed_checks = sum(
            check.passed for suite in suites for check in suite.checks
        )
        return {
            "ok": all(suite.passed for suite in suites),
            "environment": {
                "database": "temporary SQLite per suite",
                "short_term_memory": "in-memory Redis-compatible store",
                "model_provider": "deterministic mock",
                "filesystem_outputs": "temporary directory per suite",
            },
            "summary": {
                "suite_count": len(suites),
                "passed_suites": sum(suite.passed for suite in suites),
                "check_count": check_count,
                "passed_checks": passed_checks,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            },
            "suites": [asdict(suite) for suite in suites],
        }


def render_summary(report: dict) -> str:
    lines = [
        "MindNook Engineering Harness",
        f"Result: {'PASS' if report['ok'] else 'FAIL'}",
        (
            f"Suites: {report['summary']['passed_suites']}/"
            f"{report['summary']['suite_count']} | Checks: "
            f"{report['summary']['passed_checks']}/{report['summary']['check_count']} | "
            f"Duration: {report['summary']['duration_ms']} ms"
        ),
    ]
    for suite in report["suites"]:
        lines.append(
            f"[{'PASS' if suite['passed'] else 'FAIL'}] {suite['name']} "
            f"({suite['duration_ms']} ms)"
        )
        for check in suite["checks"]:
            lines.append(
                f"  [{'PASS' if check['passed'] else 'FAIL'}] {check['name']}: "
                f"{check['detail']}"
            )
    return "\n".join(lines)


def report_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
