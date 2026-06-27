from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
    coverage_status: Literal["ready", "partial", "failed"] = "partial"


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
    relationship: Literal["primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates", "background_for"]
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


class CorpusGap(BaseModel):
    gap_id: str
    kind: Literal[
        "triage_not_ready",
        "source_map_gap",
        "missing_function",
        "coverage",
        "research_audit",
        "memo_evidence",
        "treatment",
        "duration",
    ]
    description: str
    required_editorial_functions: list[str] = Field(default_factory=list)
    related_source_ids: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "medium"
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
    status: Literal["accepted", "rejected", "duplicate", "failed", "ingested"] = "accepted"
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


class EditorialAtom(BaseModel):
    kind: Literal[
        "conflict",
        "mechanism",
        "history",
        "turning_point",
        "consequence",
        "exception",
        "human_cost",
        "institutional_incentive",
        "unanswered_question",
    ]
    title: str
    explanation: str
    source_ids: list[str] = Field(default_factory=list)
    quote_refs: list[str] = Field(default_factory=list)
    story_value: str
    anchor_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    risk: str = "medium"
    source_coverage_status: Literal["ready", "partial", "weak"] = "partial"


class ResearchMemo(BaseModel):
    title: str
    thesis: str
    research_synthesis: str
    editorial_atoms: list[EditorialAtom]
    source_coverage: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risk_register: list[str] = Field(default_factory=list)


class ResearchAudit(BaseModel):
    passed: bool
    score: float = Field(ge=0, le=1)
    verdict: str
    missing_material: list[str] = Field(default_factory=list)
    required_followups: list[str] = Field(default_factory=list)


class TreatmentAct(BaseModel):
    act_number: int
    title: str
    story_role: str
    opening_question: str
    main_turn: str
    ending_turn: str
    source_focus: list[str] = Field(default_factory=list)
    target_minutes: float
    required_atom_refs: list[str] = Field(default_factory=list)
    required_quote_refs: list[str] = Field(default_factory=list)
    required_evidence_ids: list[str] = Field(default_factory=list)
    source_density_target: int = 2


class Treatment(BaseModel):
    title: str
    logline: str
    host_dynamic: str
    acts: list[TreatmentAct]
    recurring_questions: list[str] = Field(default_factory=list)
    style_constraints: list[str] = Field(default_factory=list)


class ActDossier(BaseModel):
    act_number: int
    title: str
    core_question: str
    must_use_sources: list[str] = Field(default_factory=list)
    quote_cards: list[QuoteCard] = Field(default_factory=list)
    anchor_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    continuity_constraints: list[str] = Field(default_factory=list)
    claim_risks: list[str] = Field(default_factory=list)
    source_gaps: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    host_instructions: dict = Field(default_factory=dict)
    source_density_minimum: int = 2


class DialogueLine(BaseModel):
    speaker: Literal["A", "B"]
    text: str
    ssml_hints: dict = Field(default_factory=dict)
    claim_refs: list[str] = Field(default_factory=list)


class ActDraft(BaseModel):
    act_number: int
    title: str
    lines: list[DialogueLine]
    used_source_ids: list[str] = Field(default_factory=list)
    repeated_phrase_watchlist: list[str] = Field(default_factory=list)


class ContinuityLedger(BaseModel):
    introduced_entities: list[str] = Field(default_factory=list)
    unresolved_promises: list[str] = Field(default_factory=list)
    used_analogies: list[str] = Field(default_factory=list)
    repeated_phrases: list[str] = Field(default_factory=list)
    citation_sensitive_claims: list[str] = Field(default_factory=list)


class EpisodeCritique(BaseModel):
    passed: bool
    summary: str
    repetition_issues: list[str] = Field(default_factory=list)
    pacing_issues: list[str] = Field(default_factory=list)
    weak_sections: list[str] = Field(default_factory=list)
    rewrite_instructions: list[str] = Field(default_factory=list)


class DraftClaim(BaseModel):
    claim_id: str
    statement: str
    act_number: int
    line_index: int = 0
    text_span: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    claim_type: Literal["factual", "interpretive", "quote", "causal", "statistical", "historical"] = "factual"
    source_need: Literal["required", "recommended", "none"] = "required"
    risk_level: Literal["low", "medium", "high"] = "medium"
    support_level: Literal["supported", "weak", "unsupported"] = "weak"


class CitationLink(BaseModel):
    claim_id: str
    source_id: str
    source_version_id: str | None = None
    evidence_id: str | None = None
    quote: str | None = None
    locator: str
    representation_id: str | None = None
    quote_card_ref: str | None = None
    supports_span: str = ""
    confidence: float = Field(default=0.6, ge=0, le=1)


class FactIssue(BaseModel):
    claim_id: str
    act_number: int
    issue: str
    repair: str
    severity: Literal["low", "medium", "high"] = "medium"
    blocking: bool = True


class CitationValidationReport(BaseModel):
    passed: bool
    issues: list[FactIssue] = Field(default_factory=list)
    checked_claim_count: int = 0
    checked_link_count: int = 0
    supported_required_claim_count: int = 0
    weak_required_claim_count: int = 0
    missing_required_link_count: int = 0
    invalid_link_count: int = 0
    quote_match_count: int = 0
    quote_mismatch_count: int = 0
    low_confidence_link_count: int = 0
    nonblocking_low_issue_count: int = 0
    blocking_issue_count: int = 0
    support_judgment_count: int = 0
    unsupported_judgment_count: int = 0


class SupportJudgment(BaseModel):
    claim_id: str
    evidence_id: str
    supported: bool
    support_level: Literal["supported", "weak", "unsupported", "overstrong", "needs_attribution"] = "unsupported"
    rationale: str
    required_repair: str = ""
    blocking: bool = True


class SupportJudgmentBundle(BaseModel):
    judgments: list[SupportJudgment] = Field(default_factory=list)


class ClaimCitationBundle(BaseModel):
    claims: list[DraftClaim]
    citation_links: list[CitationLink]
    fact_issues: list[FactIssue] = Field(default_factory=list)


class FinalTranscript(BaseModel):
    title: str
    description: str
    lines: list[DialogueLine]
    quality_metrics: dict = Field(default_factory=dict)


class ShowBibleContext(BaseModel):
    show_style: str = "Conversational"
    tone_and_mood: str = "conversational"
    hosts: list[dict] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    rigor_policy: str | None = None
    adjacency_policy: str | None = None
    explanation_policy: str | None = None
    format_rules: dict = Field(default_factory=dict)
