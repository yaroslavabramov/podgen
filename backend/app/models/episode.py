import uuid
from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("generation_runs.id", ondelete="CASCADE"), nullable=False, unique=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(5), nullable=False, default="en")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audio_file_path: Mapped[str | None] = mapped_column(String(512))
    transcript_json: Mapped[list | None] = mapped_column(JSONB)
    shownotes_json: Mapped[dict | None] = mapped_column(JSONB)
    quality_metrics: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    project: Mapped["Project"] = relationship(back_populates="episodes")
