from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.generation_v2.contracts.source import (
    CorpusBriefAuditIssueCode,
    CorpusBriefAuditSeverity,
    CorpusBriefSourceSetStatus,
    CorpusBriefSourceSetKind,
    CoverageNextAction,
    CoverageOutcomeStatus,
    CoverageQuestionImportance,
    CoverageQuestionKind,
    CoverageQuestionOrigin,
    ResearchHandoffAction,
    SourceCardCoverageStatus,
    SourceContributionUniqueness,
    SourceDisposition,
    SourceEvidenceAuthority,
    SourceProcessingStatus,
    SourceRepresentationStatus,
    SourceRelationshipKind,
    SourceTopicRelevance,
)


class SourceProfile(BaseModel):
    """Topic-neutral, project-level orientation for one immutable source version."""

    schema_version: str = "source_profile.v2"
    project_id: str
    source_id: str
    source_version_id: str
    neutral_summary: str = ""
    outline_hints: list[str] = Field(default_factory=list)
    source_kind: str = "unknown"
    publisher: str = ""
    language: str = ""
    published_at: str | None = None
    representation_preference: str = ""
    readability_warnings: list[str] = Field(default_factory=list)
    stable_cautions: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    duplicate_fingerprint: str = ""
    profile_origin: str = "generated"


class CoverageQuestion(BaseModel):
    question_id: str
    question: str
    kind: CoverageQuestionKind = CoverageQuestionKind.OTHER
    importance: CoverageQuestionImportance = CoverageQuestionImportance.SUPPORTING
    rationale: str = ""
    parent_question_id: str | None = None
    origin: CoverageQuestionOrigin = CoverageQuestionOrigin.PLANNED


class SourceAssessment(BaseModel):
    """Run-specific source judgment without collapsing independent axes into one score."""

    assessment_id: str
    source_id: str
    source_version_ids: list[str] = Field(default_factory=list)
    source_profile_id: str | None = None
    processing_status: SourceProcessingStatus = SourceProcessingStatus.RESOLVED
    representation_status: SourceRepresentationStatus = SourceRepresentationStatus.UNCHECKED
    representation_reason: str = ""
    relevance: SourceTopicRelevance = SourceTopicRelevance.UNKNOWN
    relevance_reason: str = ""
    authority: SourceEvidenceAuthority = SourceEvidenceAuthority.UNKNOWN
    authority_reason: str = ""
    uniqueness: SourceContributionUniqueness = SourceContributionUniqueness.UNKNOWN
    uniqueness_reason: str = ""
    disposition: SourceDisposition = SourceDisposition.BACKGROUND
    disposition_reason: str = ""
    coverage_question_ids: list[str] = Field(default_factory=list)
    editorial_functions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class CoverageOutcome(BaseModel):
    outcome_id: str
    question_id: str
    status: CoverageOutcomeStatus = CoverageOutcomeStatus.UNKNOWN
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    orientation_summary: str = ""
    missing_detail: str = ""
    conflict_summary: str = ""
    deferred_reason: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    next_action: CoverageNextAction = CoverageNextAction.RESEARCH


class CorpusBriefIntent(BaseModel):
    user_prompt: str
    central_question: str = ""
    language: str = ""
    duration_seconds: int | None = Field(default=None, ge=0)
    constraints: list[str] = Field(default_factory=list)


class CorpusBriefSourceSet(BaseModel):
    set_id: str
    set_kind: CorpusBriefSourceSetKind = CorpusBriefSourceSetKind.COVERAGE
    label: str
    purpose: str
    rationale: str = ""
    source_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    priority: str = "normal"
    status: CorpusBriefSourceSetStatus = CorpusBriefSourceSetStatus.MAPPED
    findings_summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    deferred_reason: str = ""


class ResearchHandoffItem(BaseModel):
    handoff_id: str
    priority: str = "normal"
    objective: str
    question_ids: list[str] = Field(default_factory=list)
    preferred_source_ids: list[str] = Field(default_factory=list)
    starting_evidence_ids: list[str] = Field(default_factory=list)
    action: ResearchHandoffAction = ResearchHandoffAction.DEEP_READ
    reason: str = ""


class SourceGapRecord(BaseModel):
    """Structured lineage for a triage gap while legacy text lists stay readable."""

    gap_id: str
    kind: str = "gap"
    description: str
    source_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CorpusBrief(BaseModel):
    schema_version: str = "corpus_brief.v1"
    generation_run_id: str
    project_id: str
    run_corpus_view_id: str
    corpus_revision: int = Field(ge=1)
    attempt: int = Field(ge=1)
    source_map_artifact_id: str
    source_map_hash: str
    intent: CorpusBriefIntent
    coverage_questions: list[CoverageQuestion] = Field(default_factory=list)
    source_sets: list[CorpusBriefSourceSet] = Field(default_factory=list)
    coverage_outcomes: list[CoverageOutcome] = Field(default_factory=list)
    research_handoff: list[ResearchHandoffItem] = Field(default_factory=list)
    gap_records: list[SourceGapRecord] = Field(default_factory=list)
    summary: str = ""
    cautions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_internal_references(self) -> "CorpusBrief":
        question_ids = _unique_ids(self.coverage_questions, "question_id", "coverage question")
        _unique_ids(self.source_sets, "set_id", "source set")
        _unique_ids(self.coverage_outcomes, "outcome_id", "coverage outcome")
        _unique_ids(self.research_handoff, "handoff_id", "research handoff item")
        _unique_ids(self.gap_records, "gap_id", "gap record")
        outcome_question_ids = [item.question_id for item in self.coverage_outcomes]
        if len(outcome_question_ids) != len(set(outcome_question_ids)):
            raise ValueError("coverage outcomes must be unique per question_id")
        referenced_question_ids = {
            *(question_id for item in self.source_sets for question_id in item.question_ids),
            *(item.question_id for item in self.coverage_outcomes),
            *(question_id for item in self.research_handoff for question_id in item.question_ids),
            *(question_id for item in self.gap_records for question_id in item.question_ids),
        }
        unknown = sorted(referenced_question_ids - question_ids)
        if unknown:
            raise ValueError(f"unknown coverage question ids: {', '.join(unknown)}")
        for question in self.coverage_questions:
            if question.parent_question_id and question.parent_question_id not in question_ids:
                raise ValueError(f"unknown parent coverage question id: {question.parent_question_id}")
        return self


class CorpusBriefAuditIssue(BaseModel):
    issue_id: str
    code: CorpusBriefAuditIssueCode = CorpusBriefAuditIssueCode.OTHER
    severity: CorpusBriefAuditSeverity = CorpusBriefAuditSeverity.BLOCKING
    message: str
    question_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    suggested_repair: str = ""


class CorpusBriefAuditReport(BaseModel):
    schema_version: str = "corpus_brief_audit.v1"
    corpus_brief_artifact_id: str
    source_map_artifact_id: str
    passed: bool
    reported_passed: bool | None = None
    summary: str
    issues: list[CorpusBriefAuditIssue] = Field(default_factory=list)
    checked_source_count: int = Field(default=0, ge=0)
    checked_question_count: int = Field(default=0, ge=0)
    checked_evidence_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def compute_effective_pass(self) -> "CorpusBriefAuditReport":
        if self.reported_passed is None:
            self.reported_passed = self.passed
        if any(issue.severity == CorpusBriefAuditSeverity.BLOCKING for issue in self.issues):
            self.passed = False
        return self


def _unique_ids(items: list[BaseModel], field_name: str, label: str) -> set[str]:
    values = [str(getattr(item, field_name)) for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label} ids")
    return set(values)


class SourceCard(BaseModel):
    source_id: str
    source_version_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_profile_id: str | None = None
    coverage_question_ids: list[str] = Field(default_factory=list)
    editorial_functions: list[str] = Field(default_factory=list)
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
    coverage_question_ids: list[str] = Field(default_factory=list)


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
    coverage_question_ids: list[str] = Field(default_factory=list)


class SourceRelationship(BaseModel):
    source_id: str
    related_source_id: str
    relationship: SourceRelationshipKind
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    coverage_question_ids: list[str] = Field(default_factory=list)


class SourceTriageReport(BaseModel):
    ranked_sources: list[SourceTriageItem] = Field(default_factory=list)
    top_sources_by_function: dict[str, list[str]] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    source_clusters: list[SourceCluster] = Field(default_factory=list)
    source_relationships: list[SourceRelationship] = Field(default_factory=list)
    top_evidence_by_function: dict[str, list[str]] = Field(default_factory=dict)


class SourceMap(BaseModel):
    schema_version: str = "source_map.v1"
    source_cards: list[SourceCard] = Field(default_factory=list)
    quote_cards: list[QuoteCard] = Field(default_factory=list)
    source_triage: SourceTriageReport = Field(default_factory=SourceTriageReport)
    evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    gap_records: list[SourceGapRecord] = Field(default_factory=list)
    retrieval_notes: list[str] = Field(default_factory=list)
    source_assessments: list[SourceAssessment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_gap_records(self) -> "SourceMap":
        _unique_ids(self.gap_records, "gap_id", "gap record")
        return self


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
    unresolved_source_refs: list[str] = Field(default_factory=list)
    workspace_violations: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "CorpusBrief",
    "CorpusBriefAuditIssue",
    "CorpusBriefAuditReport",
    "CorpusBriefIntent",
    "CorpusBriefSourceSet",
    "CoverageOutcome",
    "CoverageQuestion",
    "QuoteCard",
    "ResearchHandoffItem",
    "ResearchWorkflowCompletion",
    "SourceAssessment",
    "SourceCard",
    "SourceCluster",
    "SourceGapRecord",
    "SourceMap",
    "SourceRelationship",
    "SourceTriageCompletion",
    "SourceTriageGate",
    "SourceTriageItem",
    "SourceTriageReport",
    "SourceProfile",
]
