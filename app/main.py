from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.agents.runtime import CoordinatorAgent
from app.account_schema import migrate_account_schema
from app.api import admin, auth, student
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db import Base, build_engine, build_session_factory
from app.models import User
from app.risk_levels import migrate_legacy_risk_levels
from app.harness.agent_harness import AgentHarness
from app.memory.private import AgentPrivateMemory
from app.memory.service import ShortTermMemoryService
from app.memory.store import build_redis_store
from app.memory.trace import AgentTraceService
from app.rag.service import get_rag_service
from app.services.chat import ChatService
from app.services.model import build_model_provider
from app.services.reporting import backfill_handoff_summaries
from app.services.tool_dispatcher import InlineToolDispatcher, ThreadedToolDispatcher
from app.tools.operations import ToolOperations


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _seed_demo_users(session_factory, settings: Settings) -> None:
    db = session_factory()
    try:
        seeds = (
            (
                settings.demo_student_username,
                settings.demo_student_password,
                "同学",
                "student",
            ),
            (
                settings.demo_admin_username,
                settings.demo_admin_password,
                "管理员",
                "admin",
            ),
        )
        for institutional_id, password, name, role in seeds:
            existing = db.scalar(
                select(User).where(User.institutional_id == institutional_id)
            )
            if not existing:
                db.add(
                    User(
                        institutional_id=institutional_id,
                        name=name,
                        password_hash=hash_password(password),
                        role=role,
                    )
                )
        db.commit()
    finally:
        db.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    engine = build_engine(app_settings.database_url)
    session_factory = build_session_factory(engine)
    model_provider = build_model_provider(app_settings)
    rag_service = get_rag_service(app_settings)
    agent_runtime = CoordinatorAgent(
        model_provider,
        app_settings,
        rag_service=rag_service,
    )
    redis_store = build_redis_store(app_settings)
    memory_service = ShortTermMemoryService(app_settings, redis_store)
    agent_private_memory = AgentPrivateMemory(app_settings, redis_store)
    trace_service = AgentTraceService()
    tool_dispatcher = (
        InlineToolDispatcher(session_factory, app_settings)
        if app_settings.app_env == "test"
        else ThreadedToolDispatcher(session_factory, app_settings)
    )
    tool_operations = ToolOperations(session_factory, app_settings)
    agent_harness = AgentHarness(
        session_factory,
        agent_runtime,
        tool_dispatcher,
        memory_service,
        agent_private_memory,
        trace_service,
    )
    chat_service = ChatService(agent_harness)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        migrate_account_schema(engine)
        migrate_legacy_risk_levels(engine)
        Base.metadata.create_all(engine)
        _seed_demo_users(session_factory, app_settings)
        backfill_handoff_summaries(session_factory, agent_runtime.skill_registry)
        tool_dispatcher.start()
        yield
        close_provider = getattr(model_provider, "close", None)
        if callable(close_provider):
            close_provider()
        tool_dispatcher.close()
        close_redis = getattr(redis_store, "close", None)
        if callable(close_redis):
            close_redis()
        engine.dispose()

    application = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.model_provider = model_provider
    application.state.agent_runtime = agent_runtime
    application.state.rag_service = rag_service
    application.state.skill_registry = agent_runtime.skill_registry
    application.state.coordinator_agent = agent_runtime
    application.state.agent_harness = agent_harness
    application.state.chat_service = chat_service
    application.state.tool_dispatcher = tool_dispatcher
    application.state.tool_operations = tool_operations
    application.state.redis_store = redis_store
    application.state.memory_service = memory_service
    application.state.agent_private_memory = agent_private_memory
    application.state.agent_trace_service = trace_service

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    application.include_router(auth.router)
    application.include_router(student.router)
    application.include_router(admin.router)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/student")

    @application.get("/student", include_in_schema=False)
    def student_page():
        return FileResponse(STATIC_DIR / "student" / "index.html")

    @application.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC_DIR / "admin" / "index.html")

    @application.get("/health/live", tags=["health"])
    def health_live():
        return {"status": "ok"}

    @application.get("/health/ready", tags=["health"])
    def health_ready():
        return {
            "status": "ok",
            "database": "ready",
            "model_provider": app_settings.model_provider,
            "model_name": app_settings.model_name,
            "agent_runtime": "ready",
            "agent_harness": "ready",
            "tool_queue": "ready",
            "memory": "ready",
        }

    return application


app = create_app()
