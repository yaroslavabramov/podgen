from __future__ import annotations

from pydantic import BaseModel, Field

from app.generation_v2.contracts.source import SourceCardCoverageStatus, SourceRelationshipKind


class SourceCard(BaseModel):
    source_id: str
    source_version_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    title: str
    source_type: str
    url: str | None = None
    domain: str | None = None
    published_at: str | None = None
    length_hint: str = ""
    provider_status: str = "unknown"
    reliability: str = "unknown"
    authority: str = "unknown"
    summary: str
    useful_for: list[str] = Field(default_factory=list)
    gaps_or_cautions: list[str] = Field(default_factory=list)
    representation_ids: list[str] = Field(default_factory=list)
    representation_summary: str = ""
    ready_representation_count: int = 0
    failed_representation_count: int = 0
    coverage_status: SourceCardCoverageStatus = SourceCardCoverageStatus.PARTIAL


class QuoteCard(BaseModel):
    source_id: str
    source_version_id: str | None = None
    evidence_id: str | None = None
    quote: str
    locator: str
    narrative_use: str
    representation_id: str | None = None
    source_kind: str = "unknown"
    confidence: float = Field(default=0.7, ge=0, le=1)
    confidence_reason: str = ""


class SourceTriageItem(BaseModel):
    source_id: str
    source_version_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    rank: int
    score: float
    authority: str = "unknown"
    reliability: str = "unknown"
    useful_for: list[str] = Field(default_factory=list)
    top_quote_refs: list[str] = Field(default_factory=list)
    relevance_reasons: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class SourceCluster(BaseModel):
    cluster_id: str
    label: str
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    primary_source_ids: list[str] = Field(default_factory=list)
    secondary_source_ids: list[str] = Field(default_factory=list)
    duplicate_source_ids: list[str] = Field(default_factory=list)


class SourceRelationship(BaseModel):
    source_id: str
    related_source_id: str
    relationship: SourceRelationshipKind
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class SourceTriageReport(BaseModel):
    ranked_sources: list[SourceTriageItem] = Field(default_factory=list)
    top_sources_by_function: dict[str, list[str]] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    source_clusters: list[SourceCluster] = Field(default_factory=list)
    source_relationships: list[SourceRelationship] = Field(default_factory=list)
    top_evidence_by_function: dict[str, list[str]] = Field(default_factory=dict)


class SourceMap(BaseModel):
    source_cards: list[SourceCard] = Field(default_factory=list)
    quote_cards: list[QuoteCard] = Field(default_factory=list)
    source_triage: SourceTriageReport = Field(default_factory=SourceTriageReport)
    evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    retrieval_notes: list[str] = Field(default_factory=list)


class SourceTriageCompletion(BaseModel):
    ready: bool
    summary: str
    notes: list[str] = Field(default_factory=list)


class ResearchWorkflowCompletion(BaseModel):
    ready: bool
    summary: str
    notes: list[str] = Field(default_factory=list)


class SourceTriageGate(BaseModel):
    passed: bool
    summary: str
    tool_call_count: int = 0
    checked_source_count: int = 0
    checked_evidence_count: int = 0
    invalid_source_ids: list[str] = Field(default_factory=list)
    invalid_source_version_ids: list[str] = Field(default_factory=list)
    invalid_evidence_ids: list[str] = Field(default_factory=list)
    missing_source_card_evidence_source_ids: list[str] = Field(default_factory=list)
    missing_model_read_source_ids: list[str] = Field(default_factory=list)
    missing_source_card_write_source_ids: list[str] = Field(default_factory=list)
    unpersisted_completion_gap_notes: list[str] = Field(default_factory=list)
    ungrounded_relationships: list[str] = Field(default_factory=list)
    missing_function_evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "QuoteCard",
    "ResearchWorkflowCompletion",
    "SourceCard",
    "SourceCluster",
    "SourceMap",
    "SourceRelationship",
    "SourceTriageCompletion",
    "SourceTriageGate",
    "SourceTriageItem",
    "SourceTriageReport",
]
