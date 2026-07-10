from __future__ import annotations

from pydantic import BaseModel, Field

from app.generation_v2.contracts.source import CorpusGapKind, SeverityLevel, SourceAcquisitionCandidateStatus


class CorpusGap(BaseModel):
    gap_id: str
    kind: CorpusGapKind
    description: str
    required_editorial_functions: list[str] = Field(default_factory=list)
    related_source_ids: list[str] = Field(default_factory=list)
    severity: SeverityLevel = SeverityLevel.MEDIUM
    origin: str = ""


class CorpusGapReport(BaseModel):
    run_id: str
    project_id: str
    round_number: int = 0
    trigger_phase: str
    target_duration_seconds: int
    summary: str
    gaps: list[CorpusGap] = Field(default_factory=list)
    required_editorial_functions: list[str] = Field(default_factory=list)
    source_count: int = 0
    evidence_count: int = 0
    should_acquire: bool = True


class SourceAcquisitionCandidate(BaseModel):
    url: str
    normalized_url: str = ""
    gap_ids: list[str] = Field(default_factory=list)
    expected_editorial_function: str = ""
    rationale: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    source_type_hint: str = "web"
    authority_hint: str = "unknown"
    status: SourceAcquisitionCandidateStatus = SourceAcquisitionCandidateStatus.ACCEPTED
    rejection_reason: str = ""
    source_id: str | None = None
    source_version_ids: list[str] = Field(default_factory=list)
    provider_file_count: int = 0
    ready_provider_file_count: int = 0
    ingest_status: str = "pending"
    fully_ingested: bool = False
    metadata: dict = Field(default_factory=dict)


class SourceAcquisitionRound(BaseModel):
    round_number: int
    trigger_phase: str
    gap_report_id: str | None = None
    accepted_count: int = 0
    rejected_count: int = 0
    failed_count: int = 0
    duplicate_count: int = 0
    ingested_source_ids: list[str] = Field(default_factory=list)
    candidate_urls: list[str] = Field(default_factory=list)
    unresolved_gap_ids: list[str] = Field(default_factory=list)
    summary: str = ""


class SourceAcquisitionReport(BaseModel):
    enabled: bool
    mode: str = "auto"
    exhausted: bool = False
    total_rounds: int = 0
    total_accepted: int = 0
    total_rejected: int = 0
    total_failed: int = 0
    total_duplicates: int = 0
    ingested_source_ids: list[str] = Field(default_factory=list)
    unresolved_gap_ids: list[str] = Field(default_factory=list)
    summary: str = ""


class SourceAcquisitionCompletion(BaseModel):
    ready: bool
    summary: str
    searched_gap_ids: list[str] = Field(default_factory=list)
    accepted_urls: list[str] = Field(default_factory=list)
    rejected_urls: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


__all__ = [
    "CorpusGap",
    "CorpusGapReport",
    "SourceAcquisitionCandidate",
    "SourceAcquisitionCompletion",
    "SourceAcquisitionReport",
    "SourceAcquisitionRound",
]
