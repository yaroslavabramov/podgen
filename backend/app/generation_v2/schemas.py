from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceCard(BaseModel):
    source_id: str
    title: str
    source_type: str
    url: str | None = None
    reliability: str = "unknown"
    authority: str = "unknown"
    summary: str
    useful_for: list[str] = Field(default_factory=list)
    gaps_or_cautions: list[str] = Field(default_factory=list)
    representation_ids: list[str] = Field(default_factory=list)


class QuoteCard(BaseModel):
    source_id: str
    quote: str
    locator: str | None = None
    narrative_use: str
    confidence: float = Field(default=0.7, ge=0, le=1)


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


class ResearchMemo(BaseModel):
    title: str
    thesis: str
    narrative_spine: str
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
    continuity_constraints: list[str] = Field(default_factory=list)
    claim_risks: list[str] = Field(default_factory=list)


class DialogueLine(BaseModel):
    speaker: Literal["A", "B"]
    text: str
    ssml_hints: dict = Field(default_factory=dict)


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
    support_level: Literal["supported", "weak", "unsupported"] = "weak"


class CitationLink(BaseModel):
    claim_id: str
    source_id: str
    quote: str | None = None
    locator: str | None = None
    confidence: float = Field(default=0.6, ge=0, le=1)


class FactIssue(BaseModel):
    claim_id: str
    act_number: int
    issue: str
    repair: str
    severity: Literal["low", "medium", "high"] = "medium"


class ClaimCitationBundle(BaseModel):
    claims: list[DraftClaim]
    citation_links: list[CitationLink]
    fact_issues: list[FactIssue] = Field(default_factory=list)


class FinalTranscript(BaseModel):
    title: str
    description: str
    lines: list[DialogueLine]
    quality_metrics: dict = Field(default_factory=dict)
