import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import String, DateTime, ForeignKey, Text, Boolean, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SourceType(StrEnum):
    PDF = "pdf"
    WEB = "web"
    VIDEO = "video"
    AUDIO = "audio"


class IngestionStatus(StrEnum):
    PENDING = "pending"
    ENQUEUED = "enqueued"      # Celery task dispatched, waiting for worker pickup
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DomainReliability(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(SAEnum(SourceType, name="source_type", create_constraint=False, native_enum=False, length=20), nullable=False)
    original_url: Mapped[str | None] = mapped_column(String(2048))
    file_path: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    domain_reliability: Mapped[str] = mapped_column(
        SAEnum(DomainReliability, name="domain_reliability", create_constraint=False, native_enum=False, length=20),
        default=DomainReliability.UNKNOWN,
    )
    ingestion_status: Mapped[str] = mapped_column(
        SAEnum(IngestionStatus, name="ingestion_status", create_constraint=False, native_enum=False, length=20),
        default=IngestionStatus.PENDING,
    )
    is_temporary: Mapped[bool] = mapped_column(Boolean, default=False)
    fully_ingested: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_text_path: Mapped[str | None] = mapped_column(String(512))
    text_preview: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    generation_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("generation_runs.id", ondelete="SET NULL"))
    is_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status_changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project: Mapped["Project"] = relationship(back_populates="sources")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="source", cascade="all, delete-orphan")
