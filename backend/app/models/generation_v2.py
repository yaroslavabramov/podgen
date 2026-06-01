import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SourceRepresentationKind(StrEnum):
    ORIGINAL_FILE = "original_file"
    ORIGINAL_URL = "original_url"
    FETCHED_HTML = "fetched_html"
    RENDERED_SNAPSHOT = "rendered_snapshot"
    EXTRACTED_TEXT = "extracted_text"
    PAGE_IMAGE = "page_image"
    PROVIDER_FILE = "provider_file"
    PROVIDER_VECTOR_STORE = "provider_vector_store"
    TRANSCRIPT = "transcript"


class V2RecordStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class V2ArtifactStatus(StrEnum):
    DRAFT = "draft"
    PASSED = "passed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class V2ArtifactType(StrEnum):
    SOURCE_CARD = "source_card"
    QUOTE_CARD = "quote_card"
    EDITORIAL_ATOM = "editorial_atom"
    RESEARCH_MEMO = "research_memo"
    RESEARCH_AUDIT = "research_audit"
    TREATMENT = "treatment"
    ACT_DOSSIER = "act_dossier"
    ACT_DRAFT = "act_draft"
    CONTINUITY_LEDGER = "continuity_ledger"
    EPISODE_CRITIQUE = "episode_critique"
    DRAFT_CLAIM = "draft_claim"
    CITATION_LINK = "citation_link"
    FACT_ISSUE = "fact_issue"
    FINAL_TRANSCRIPT = "final_transcript"


class SourceRepresentation(Base):
    __tablename__ = "source_representations"
    __table_args__ = (
        Index("ix_source_representations_source_kind", "source_id", "kind"),
        Index("ix_source_representations_provider_file", "provider", "provider_file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_file_id: Mapped[str | None] = mapped_column(String(255))
    provider_vector_store_id: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2RecordStatus.PENDING)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CorpusWorkspace(Base):
    __tablename__ = "corpus_workspaces"
    __table_args__ = (
        Index("ix_corpus_workspaces_project_run", "project_id", "generation_run_id"),
        Index("ix_corpus_workspaces_source_set_hash", "source_set_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_vector_store_id: Mapped[str | None] = mapped_column(String(255))
    source_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2RecordStatus.PENDING)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class V2Artifact(Base):
    __tablename__ = "v2_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "generation_run_id",
            "artifact_type",
            "artifact_key",
            "version",
            name="uq_v2_artifact_version",
        ),
        Index("ix_v2_artifacts_run_type", "generation_run_id", "artifact_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_artifact_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    model: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2ArtifactStatus.DRAFT)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class V2ToolTrace(Base):
    __tablename__ = "v2_tool_traces"
    __table_args__ = (
        Index("ix_v2_tool_traces_run_phase", "generation_run_id", "phase"),
        Index("ix_v2_tool_traces_provider_call", "provider_call_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phase: Mapped[str] = mapped_column(String(80), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provider_call_id: Mapped[str | None] = mapped_column(String(255))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
