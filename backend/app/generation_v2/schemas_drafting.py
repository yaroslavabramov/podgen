from __future__ import annotations

from pydantic import BaseModel, Field

from app.generation_v2.contracts.citation import CitationSupportLevel, DialogueSpeaker, DraftClaimType, SourceNeed
from app.generation_v2.contracts.source import SeverityLevel
from app.generation_v2.schemas_source import QuoteCard


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
    speaker: DialogueSpeaker
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
    claim_type: DraftClaimType = DraftClaimType.FACTUAL
    source_need: SourceNeed = SourceNeed.REQUIRED
    risk_level: SeverityLevel = SeverityLevel.MEDIUM
    support_level: CitationSupportLevel = CitationSupportLevel.WEAK


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
    severity: SeverityLevel = SeverityLevel.MEDIUM
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
    support_level: CitationSupportLevel = CitationSupportLevel.UNSUPPORTED
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


__all__ = [
    "ActDossier",
    "ActDraft",
    "CitationLink",
    "CitationValidationReport",
    "ClaimCitationBundle",
    "ContinuityLedger",
    "DialogueLine",
    "DraftClaim",
    "EpisodeCritique",
    "FactIssue",
    "FinalTranscript",
    "ShowBibleContext",
    "SupportJudgment",
    "SupportJudgmentBundle",
]
