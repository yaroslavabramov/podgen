from pydantic_settings import BaseSettings


def _strip_sqlalchemy_driver(url: str) -> str:
    """Convert SQLAlchemy-style URLs to libpq-compatible sync URLs.

    Example:
    - postgresql+asyncpg://... -> postgresql://...
    - postgresql://... -> postgresql://...
    """
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "+" in scheme:
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}://{rest}"


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://podgen:podgen@localhost:5432/podgen"
    database_url_sync: str | None = None

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "podgen"
    minio_secure: bool = False

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "podgen_chunks"

    # LLM
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_request_timeout_seconds: int = 300
    showrunner_generation_provider: str = "anthropic"
    showrunner_openai_model: str = "gpt-5.4"
    showrunner_openai_reasoning_effort: str = "medium"
    generation_v2_openai_model: str = "gpt-5"
    generation_v2_openai_reasoning_effort: str = "medium"
    generation_v2_enable_provider_uploads: bool = True
    generation_v2_enable_openai_url_read: bool = False
    generation_v2_enable_openai_url_recovery: bool = False
    generation_v2_openai_url_read_max_output_tokens: int = 12000
    generation_v2_openai_url_read_min_chars: int = 160
    generation_v2_openai_url_read_timeout_seconds: int = 90
    generation_v2_openai_url_recovery_timeout_seconds: int = 60
    generation_v2_enable_web_search: bool = True
    generation_v2_enable_web_search_research: bool = True
    generation_v2_enable_rendered_snapshots: bool = True
    generation_v2_cleanup_provider_files: bool = False
    generation_v2_allow_runtime_fallback: bool = False
    generation_v2_allow_fallback_audio: bool = False
    generation_v2_max_tool_rounds: int = 8
    generation_v2_triage_max_tool_rounds: int = 12
    generation_v2_debug_dump_dir: str = ""

    # LangSmith tracing
    langsmith_tracing: bool = False
    langsmith_project: str = "podgen"
    langsmith_api_key: str = ""
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_base_tags: str = "podgen,backend"
    langsmith_sampling_rate: float = 1.0

    # ElevenLabs
    elevenlabs_api_key: str = ""

    # Tavily
    tavily_api_key: str = ""

    # Jina Reader
    jina_api_key: str = ""

    # Deepgram
    deepgram_api_key: str = ""

    # Auth
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]
    cors_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

    # Governors
    max_fast_web_per_act: int = 5
    max_fast_web_initial_gaps: int = 5
    max_fast_web_verifier_gaps: int = 5
    max_deep_ingestion_concurrent: int = 3
    max_writer_revolt_per_act: int = 4
    max_writer_revolt_per_scene: int = 2
    max_writer_rewrite_loops: int = 4
    max_citation_audit_attempts: int = 2
    max_citation_repair_attempts: int = 2
    max_verifier_research_loops: int = 2
    temp_source_ttl_days: int = 7

    # Web security
    web_fetch_timeout: int = 30
    web_max_file_size_mb: int = 50
    web_quality_gate_a_min_chars: int = 160
    web_date_extraction_min_preview_chars: int = 80
    web_user_agent: str = "PodgenIngestionBot/1.0 (+https://example.com/podgen; ingest@podgen.local)"
    web_accept_language: str = "en-US,en;q=0.9"
    web_dns_resolve_retries: int = 3
    web_dns_resolve_retry_delay_ms: int = 250
    web_extractor_debug_dump_dir: str = ""
    pdf_marker_timeout_seconds: int = 300
    content_map_rebuild_debounce_seconds: int = 90

    @property
    def database_url_sync_resolved(self) -> str:
        """Sync URL for psycopg/alembic, aligned with DATABASE_URL by default."""
        if self.database_url_sync:
            return _strip_sqlalchemy_driver(self.database_url_sync)
        return _strip_sqlalchemy_driver(self.database_url)

    model_config = {"env_file": ["../.env", ".env"], "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
