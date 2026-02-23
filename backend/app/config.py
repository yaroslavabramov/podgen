from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://podgen:podgen@localhost:5432/podgen"
    database_url_sync: str = "postgresql://podgen:podgen@localhost:5432/podgen"

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
