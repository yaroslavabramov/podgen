import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import String, Text, DateTime, Float, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class GenerationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    show_bible_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("show_bibles.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        SAEnum(GenerationStatus, name="generation_status", create_constraint=False, native_enum=False, length=20),
        default=GenerationStatus.QUEUED,
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    current_phase: Mapped[str | None] = mapped_column(String(50))
    current_step: Mapped[str | None] = mapped_column(String(100))
    progress_pct: Mapped[float | None] = mapped_column(Float)
    langgraph_thread_id: Mapped[str | None] = mapped_column(String(100))
    estimated_cost: Mapped[dict | None] = mapped_column(JSONB)
    actual_cost: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    project: Mapped["Project"] = relationship(back_populates="generation_runs")
