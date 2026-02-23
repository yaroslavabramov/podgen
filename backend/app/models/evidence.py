import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import String, Text, DateTime, Float, ForeignKey, Integer, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AssetType(StrEnum):
    STATISTIC = "statistic"
    ANECDOTE = "anecdote"
    EXPERT_QUOTE = "expert_quote"
    DEFINITION = "definition"
    MECHANISM = "mechanism"
    CASE = "case"
    TIMELINE = "timeline"
    CONTRAST = "contrast"


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chunks.id"))
    verbatim_quote: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict | None] = mapped_column(JSONB)
    start_idx: Mapped[int | None] = mapped_column(Integer)
    end_idx: Mapped[int | None] = mapped_column(Integer)
    reliability_hint: Mapped[str | None] = mapped_column(String(50))
    asset_type: Mapped[str] = mapped_column(
        SAEnum(AssetType, name="asset_type", create_constraint=False, native_enum=False, length=30),
        nullable=False,
    )
    narrative_value_score: Mapped[float | None] = mapped_column(Float)
    tellability_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
