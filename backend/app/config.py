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
    max_deep_ingestion_concurrent: int = 3
    max_writer_revolt_per_act: int = 4
    max_writer_revolt_per_scene: int = 2
    max_verifier_research_loops: int = 2
    temp_source_ttl_days: int = 7

    # Web security
    web_fetch_timeout: int = 30
    web_max_file_size_mb: int = 50

    @property
    def database_url_sync_resolved(self) -> str:
        """Sync URL for psycopg/alembic, aligned with DATABASE_URL by default."""
        if self.database_url_sync:
            return _strip_sqlalchemy_driver(self.database_url_sync)
        return _strip_sqlalchemy_driver(self.database_url)

    model_config = {"env_file": ["../.env", ".env"], "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
