from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "MindNook"
    app_env: str = "development"
    app_secret: str = "mindnook-local-development-secret"
    database_url: str = "sqlite:///./mindnook.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_connect_timeout_seconds: float = 0.5

    short_term_memory_prefix: str = "mindbridge:short-term-memory"
    short_term_memory_max_messages: int = 40
    short_term_memory_ttl_seconds: int = 86400
    memory_recent_messages: int = 8
    memory_brief_max_chars: int = 500
    memory_model_history_max_chars: int = 6000
    agent_private_memory_max_entries: int = 20
    agent_private_memory_ttl_seconds: int = 86400

    rag_knowledge_dir: str = "data/knowledge"
    rag_chroma_path: str = "data/chroma"
    rag_collection_name: str = "mindnook_knowledge"
    rag_chunk_size: int = 512
    rag_chunk_overlap: int = 64
    rag_embedding_dimension: int = 384
    rag_candidate_k: int = 12
    rag_top_k: int = 3
    rag_vector_weight: float = 0.45
    rag_bm25_weight: float = 0.55
    skills_dir: str = "skills"

    model_provider: str = "mock"
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_api_key: str = ""
    model_name: str = "qwen-plus"
    model_timeout_seconds: float = 20.0
    model_max_retries: int = 1
    model_max_calls_per_run: int = 7
    model_run_timeout_seconds: float = 55.0
    agent_max_rounds: int = 16
    model_json_mode: bool = False
    model_understanding_name: str = ""
    model_safety_name: str = ""
    model_response_name: str = ""

    excel_ledger_path: str = "data/exports/mindnook-risk-ledger.xlsx"

    tool_worker_poll_seconds: float = 1.0
    tool_worker_batch_size: int = 20
    tool_worker_max_attempts: int = 3
    tool_worker_retry_base_seconds: float = 2.0
    tool_worker_running_timeout_seconds: int = 300
    tool_worker_excel_workers: int = 1
    tool_worker_case_workers: int = 2
    tool_worker_alert_workers: int = 2
    alert_rate_limit_per_minute: int = 10

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_address: str = ""
    alert_email_to: str = ""
    smtp_use_tls: bool = True

    demo_student_username: str = "student"
    demo_student_password: str = "student123"
    demo_admin_username: str = "admin"
    demo_admin_password: str = "admin123"

    access_token_ttl_seconds: int = 8 * 60 * 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
