import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
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
    PAGE_MARKED_TEXT = "page_marked_text"
    MODEL_READ_TEXT = "model_read_text"


class ProviderCorpusScope(StrEnum):
    PROJECT = "project"
    RUN = "run"


class EvidenceRetrievalChannel(StrEnum):
    PROVIDER_FILE_SEARCH = "provider_file_search"
    PROVIDER_VECTOR_SEARCH = "provider_vector_search"
    MODEL_FILE_READ = "model_file_read"
    MODEL_FILE_READ_LOCAL_FALLBACK = "model_file_read_local_fallback"
    LOCAL_CHUNK_SEARCH = "local_chunk_search"
    LOCAL_TRANSCRIPT_SEARCH = "local_transcript_search"
    WEB_SNAPSHOT = "web_snapshot"


class EvidenceLocatorType(StrEnum):
    PAGE = "page"
    SECTION = "section"
    TIMESTAMP = "timestamp"
    PROVIDER_SNIPPET = "provider_snippet"
    SOURCE_LEVEL = "source_level"
    UNKNOWN = "unknown"


class EvidenceStrength(StrEnum):
    EXACT = "exact"
    SNIPPET = "snippet"
    SOURCE_LEVEL = "source_level"
    WEAK = "weak"


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
    SOURCE_MAP = "source_map"
    SOURCE_TRIAGE = "source_triage"
    SOURCE_TRIAGE_WORKSPACE = "source_triage_workspace"
    CORPUS_GAP_REPORT = "corpus_gap_report"
    SOURCE_ACQUISITION_CANDIDATE = "source_acquisition_candidate"
    SOURCE_ACQUISITION_ROUND = "source_acquisition_round"
    SOURCE_ACQUISITION_REPORT = "source_acquisition_report"
    RESEARCH_WORKSPACE = "research_workspace"
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
    CITATION_VALIDATION = "citation_validation"
    SUPPORT_JUDGMENT = "support_judgment"
    EVIDENCE_ITEM = "evidence_item"
    FACT_ISSUE = "fact_issue"
    FINAL_TRANSCRIPT = "final_transcript"


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (
        Index("ix_source_versions_source_active", "source_id", "is_active"),
        Index("ix_source_versions_project_kind", "project_id", "kind"),
        Index("ix_source_versions_content_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_file_id: Mapped[str | None] = mapped_column(String(255))
    provider_vector_store_file_id: Mapped[str | None] = mapped_column(String(255))
    upload_status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2RecordStatus.PENDING)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2RecordStatus.PENDING)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    supersedes_source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_versions.id"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProviderCorpusIndex(Base):
    __tablename__ = "provider_corpus_indexes"
    __table_args__ = (
        UniqueConstraint("project_id", "provider", "scope", name="uq_provider_corpus_project_provider_scope"),
        Index("ix_provider_corpus_indexes_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_vector_store_id: Mapped[str | None] = mapped_column(String(255))
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default=ProviderCorpusScope.PROJECT)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2RecordStatus.PENDING)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProviderCorpusFile(Base):
    __tablename__ = "provider_corpus_files"
    __table_args__ = (
        UniqueConstraint("provider_index_id", "source_version_id", name="uq_provider_corpus_file_version"),
        Index("ix_provider_corpus_files_index_status", "provider_index_id", "status"),
        Index("ix_provider_corpus_files_source_version", "source_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_index_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_corpus_indexes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_versions.id"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_file_id: Mapped[str | None] = mapped_column(String(255))
    provider_vector_store_file_id: Mapped[str | None] = mapped_column(String(255))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2RecordStatus.PENDING)
    cleanup_status: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RunCorpusView(Base):
    __tablename__ = "run_corpus_views"
    __table_args__ = (
        UniqueConstraint(
            "generation_run_id",
            "revision",
            "attempt",
            name="uq_run_corpus_view_generation_run_revision_attempt",
        ),
        Index(
            "uq_run_corpus_views_current",
            "generation_run_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_run_corpus_views_project", "project_id"),
        Index("ix_run_corpus_views_manifest_hash", "manifest_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_index_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_corpus_indexes.id", ondelete="SET NULL"),
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("run_corpus_views.id", ondelete="SET NULL"),
    )
    selected_source_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    selected_source_version_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    active_source_filter: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    overlay_index_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    eligible_provider_file_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    readiness_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=V2RecordStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        Index("ix_evidence_items_run", "generation_run_id"),
        Index("ix_evidence_items_project_source", "project_id", "source_id"),
        Index("ix_evidence_items_source_version", "source_version_id"),
        Index("ix_evidence_items_run_scope", "generation_run_id", "corpus_revision", "attempt"),
        Index(
            "ix_evidence_items_dedupe",
            "generation_run_id",
            "corpus_revision",
            "attempt",
            "source_version_id",
            "snippet_hash",
            "retrieval_channel",
            "purpose",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    corpus_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id"),
        nullable=False,
        index=True,
    )
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_versions.id"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_vector_store_id: Mapped[str | None] = mapped_column(String(255))
    provider_file_id: Mapped[str | None] = mapped_column(String(255))
    provider_vector_store_file_id: Mapped[str | None] = mapped_column(String(255))
    response_id: Mapped[str | None] = mapped_column(String(255))
    tool_call_id: Mapped[str | None] = mapped_column(String(255))
    retrieval_channel: Mapped[str] = mapped_column(String(50), nullable=False)
    query_text: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(String(120))
    search_result_index: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    snippet_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_quote_text: Mapped[str | None] = mapped_column(Text)
    locator_text: Mapped[str | None] = mapped_column(Text)
    locator_type: Mapped[str] = mapped_column(String(50), nullable=False, default=EvidenceLocatorType.UNKNOWN)
    evidence_strength: Mapped[str] = mapped_column(String(50), nullable=False, default=EvidenceStrength.WEAK)
    snippet_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
        Index("ix_v2_artifacts_run_scope", "generation_run_id", "corpus_revision", "attempt"),
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
    corpus_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    input_artifact_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("v2_artifacts.id"))
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
        Index("ix_v2_tool_traces_run_scope", "generation_run_id", "corpus_revision", "attempt", "phase"),
        Index("ix_v2_tool_traces_provider_call", "provider_call_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    corpus_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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
