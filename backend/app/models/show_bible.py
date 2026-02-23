import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ShowBible(Base):
    __tablename__ = "show_bibles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    audience_profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tone_and_mood: Mapped[str] = mapped_column(String(50), nullable=False, default="conversational")
    show_style: Mapped[str] = mapped_column(String(50), nullable=False, default="Conversational")
    hosts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    voice_profiles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    forbidden_phrases: Mapped[list] = mapped_column(ARRAY(String), nullable=False, default=list)
    rigor_policy: Mapped[str | None] = mapped_column(Text)
    adjacency_policy: Mapped[str | None] = mapped_column(Text)
    explanation_policy: Mapped[str | None] = mapped_column(Text)
    format_rules: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    generation_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("generation_runs.id", ondelete="SET NULL"))
    is_preset: Mapped[bool] = mapped_column(Boolean, default=False)
    preset_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project: Mapped["Project"] = relationship(back_populates="show_bibles")
