import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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


class EvidenceRetrievedBy(StrEnum):
    HUNTER = "hunter"
    LIBRARIAN_FAST = "librarian_fast"
    DEEP_INGESTION = "deep_ingestion"
    ACT_PLANNER = "act_planner"


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


class EvidenceProvenance(Base):
    __tablename__ = "evidence_provenance"
    __table_args__ = (
        UniqueConstraint(
            "evidence_id",
            "generation_run_id",
            "question_id",
            "branch_id",
            "retrieved_by",
            "asset_type",
            name="uq_evidence_provenance_context",
        ),
        Index("ix_evidence_provenance_run_branch", "generation_run_id", "branch_id"),
        Index("ix_evidence_provenance_run_question", "generation_run_id", "question_id"),
        Index("ix_evidence_provenance_evidence_id", "evidence_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evidence_spans.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("episode_manifests.id", ondelete="SET NULL"),
    )
    question_id: Mapped[str] = mapped_column(String(100), nullable=False)
    branch_id: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_type: Mapped[str] = mapped_column(
        SAEnum(AssetType, name="asset_type", create_constraint=False, native_enum=False, length=30),
        nullable=False,
    )
    retrieved_by: Mapped[str] = mapped_column(
        SAEnum(
            EvidenceRetrievedBy,
            name="evidence_retrieved_by",
            create_constraint=False,
            native_enum=False,
            length=30,
        ),
        nullable=False,
    )
    query_text: Mapped[str | None] = mapped_column(Text)
    retrieval_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
