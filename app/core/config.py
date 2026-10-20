from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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
    # Recent messages are retained AFTER compaction, not a per-turn ceiling.
    memory_enabled: bool = True
    memory_summary_max_chars: int = Field(default=400, ge=100, le=2000)
    memory_history_tokens: int = Field(default=6000, ge=128)
    memory_batch_tokens: int = Field(default=5000, ge=128)
    memory_page_size: int = Field(default=200, ge=20)
    memory_foreground_max_batches: int = Field(default=3, ge=1)
    memory_model_name: str = ""
    memory_model_timeout_seconds: float = Field(default=60.0, gt=0)
    memory_batch_retry_seconds: int = Field(default=60, ge=0)
    memory_batch_max_attempts: int = Field(default=3, ge=1)
    memory_foreground_model_calls: bool = False
    memory_batch_max_messages: int = Field(default=8, ge=2, le=200)
    memory_preference_tokens: int = Field(default=400, ge=0)
    memory_raw_recall_tokens: int = Field(default=1600, ge=0)
    memory_correction_tokens: int = Field(default=800, ge=0)
    memory_raw_candidate_limit: int = Field(default=300, ge=10)
    memory_model_output_tokens: int = Field(default=2048, ge=128)
    memory_collection_name: str = "mindnook_user_memories"
    memory_candidate_k: int = Field(default=8, ge=1)
    memory_top_k: int = Field(default=3, ge=1, le=10)
    memory_min_similarity: float = Field(default=0.45, ge=0, le=1)
    memory_recall_tokens: int = Field(default=1000, ge=64)
    memory_idle_seconds: int = Field(default=300, ge=0)
    memory_worker_threads: int = Field(default=1, ge=1)
    memory_turn_lease_seconds: int = Field(default=300, ge=30)
    model_context_tokens: int = Field(default=16384, ge=1024)
    model_output_tokens: int = Field(default=1024, ge=64)
    model_context_margin_tokens: int = Field(default=512, ge=0)
    model_context_reserve_tokens: int = Field(default=4096, ge=256)
    # Per-model overrides: {"model-name": {"context_tokens": ..., "output_tokens": ...}}.
    model_context_limits: dict[str, dict[str, int]] = Field(default_factory=dict)
    agent_private_memory_max_entries: int = 20
    agent_private_memory_ttl_seconds: int = 86400

    rag_knowledge_dir: str = "data/knowledge"
    rag_chroma_path: str = "data/chroma"
    rag_collection_name: str = "mindnook_knowledge"
    rag_chunk_size: int = 512
    rag_chunk_overlap: int = 64
    rag_embedding_provider: str = "fastembed"
    rag_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    rag_embedding_cache_dir: str = "data/models"
    rag_embedding_dimension: int = 512
    rag_candidate_k: int = 12
    rag_top_k: int = 3
    rag_vector_weight: float = 0.75
    rag_bm25_weight: float = 0.25
    rag_reranker_enabled: bool = True
    rag_reranker_provider: str = "fastembed"
    rag_reranker_model: str = "BAAI/bge-reranker-base"
    rag_reranker_cache_dir: str = "data/models"
    rag_reranker_model_weight: float = 0.5
    rag_reranker_timeout_seconds: float = 30.0
    rag_reranker_batch_size: int = 12
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

    tool_worker_poll_seconds: float = 1.0
    tool_worker_batch_size: int = 20
    tool_worker_max_attempts: int = 3
    tool_worker_retry_base_seconds: float = 2.0
    tool_worker_running_timeout_seconds: int = 300
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
    smtp_use_ssl: bool = False

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
