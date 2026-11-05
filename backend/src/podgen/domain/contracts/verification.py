from typing import Literal

from pydantic import JsonValue, model_validator

from .calculations import RestrictedExpression
from .common import (
    UUID,
    ArtifactRef,
    Contract,
    DecimalString,
    DependencyDisposition,
    Finding,
    NonNegativeInt,
    RuntimeRef,
    ScriptSelector,
    ShowNotesSelector,
    SubjectSelector,
    Text,
)
from .evidence import EvidenceSelector

Verdict = Literal["supported", "supported_with_limits", "contradicted", "insufficient", "not_verifiable"]
Recommendation = Literal["accepted", "changes_required", "rejected"]


class Qualifiers(Contract):
    entities: list[str]
    time_scope: str
    geographic_scope: str
    conditions: list[str]
    modality: Text
    attribution: str | None


class Claim(Contract):
    claim_id: UUID
    statement: Text
    occurrences: list[ScriptSelector]
    kind: Literal["factual", "quantitative", "causal", "historical", "quotation", "attribution", "prediction"]
    qualifiers: Qualifiers
    importance: Literal["central", "supporting", "incidental"]
    candidate_evidence_refs: list[ArtifactRef]
    related_claim_ids: list[UUID]


class CoverageUnit(Contract):
    selector: ScriptSelector
    classification: Literal["assertion", "nonfactual"]
    claim_ids: list[UUID]
    exclusion_reason: str | None


class ExtractionQuestion(Contract):
    selector: ScriptSelector
    question: Text


class ClaimSet(Contract):
    script_ref: ArtifactRef
    claims: list[Claim]
    coverage: list[CoverageUnit]
    extraction_profile_ref: ArtifactRef
    unresolved_extraction_questions: list[ExtractionQuestion]


class CoverageAudit(Contract):
    reviewer_run_ids: list[UUID]
    reviewed_script_ranges: list[ScriptSelector]
    findings: list[Finding]
    unresolved_omissions: list[ScriptSelector]


class EvidenceReview(Contract):
    evidence_ref: ArtifactRef
    relation: Literal["supports", "qualifies", "contradicts"]
    source_applicability: Text
    original_inspection: Text
    rationale: Text


class ClaimReview(Contract):
    claim_id: UUID
    verdict: Verdict
    evidence_reviews: list[EvidenceReview]
    counterevidence_refs: list[ArtifactRef]
    calculation_refs: list[ArtifactRef]
    family_assessment_refs: list[ArtifactRef]
    stated_limits: list[str]
    audible_limit_selectors: list[ScriptSelector]
    findings: list[Finding]
    rationale: Text
    reused_from_report_ref: ArtifactRef | None


class RemainingWork(Contract):
    finding_id: UUID
    proposed_action: Text
    research_request_ref: ArtifactRef | None


class ScriptDependencyDisposition(Contract):
    prior_input_ref: ArtifactRef
    current_input_refs: list[ArtifactRef]
    affected_selectors: list[ScriptSelector]
    disposition: Literal["retained", "replaced", "removed", "lineage_only"]
    basis: Text
    checked_against_script_ref: ArtifactRef
    reviewer_run_id: UUID


class VerificationReport(Contract):
    script_ref: ArtifactRef
    claim_set_ref: ArtifactRef
    coverage_audit: CoverageAudit
    claim_reviews: list[ClaimReview]
    cross_claim_findings: list[Finding]
    source_family_assessment_refs: list[ArtifactRef]
    calculation_refs: list[ArtifactRef]
    policy_ref: ArtifactRef
    recommendation: Recommendation
    remaining_work: list[RemainingWork]
    dependency_dispositions: list[ScriptDependencyDisposition]


class EditorialDimension(Contract):
    dimension: Text
    assessment: Text
    evidence_selectors: list[ScriptSelector]
    rationale: Text


class ListenerAnswer(Contract):
    main_answer: Text
    causal_chain: list[str]
    important_limits: list[str]
    unanswered_promises: list[str]


class RevisionComparison(Contract):
    prior_script_ref: ArtifactRef
    target_finding_ids: list[UUID]
    repaired: list[str]
    preserved_strengths: list[str]
    regressions: list[str]
    preferred_action: Text


class EditorialReview(Contract):
    episode_spec_ref: ArtifactRef
    story_plan_ref: ArtifactRef
    script_ref: ArtifactRef
    listener_state_ref: ArtifactRef
    policy_ref: ArtifactRef
    first_read_ref: RuntimeRef
    spoken_view_hash: Text
    dimensions: list[EditorialDimension]
    findings: list[Finding]
    listener_answer: ListenerAnswer
    revision_comparison: RevisionComparison | None
    recommendation: Recommendation


class CalculationInput(Contract):
    name: Text
    value_decimal: DecimalString
    unit: Text
    evidence_refs: list[ArtifactRef]
    source_selector: SubjectSelector | EvidenceSelector


class Rounding(Contract):
    precision: NonNegativeInt
    mode: Text
    display_scale: NonNegativeInt


class CalculationResult(Contract):
    value_decimal: DecimalString
    unit: Text
    display_text: Text


class CalculationError(Contract):
    code: Text
    message: Text


class CalculationRecord(Contract):
    purpose: Text
    inputs: list[CalculationInput]
    expression: RestrictedExpression
    execution_profile_ref: ArtifactRef
    rounding: Rounding
    result: CalculationResult | None
    status: Literal["computed", "failed"]
    error: CalculationError | None
    limitations: list[str]

    @model_validator(mode="after")
    def outcome(self):
        if (self.status == "computed") != (self.result is not None) or (self.status == "failed") != (
            self.error is not None
        ):
            raise ValueError("calculation_outcome_mismatch")
        return self


class Chapter(Contract):
    chapter_id: UUID
    title: Text
    summary: Text
    start_frame: NonNegativeInt
    script_selectors: list[ScriptSelector]


class ShowCitation(Contract):
    citation_id: UUID
    source_revision_ref: ArtifactRef
    evidence_refs: list[ArtifactRef]
    display_metadata: dict[str, JsonValue]
    public_locator: str | None
    supported_script_selectors: list[ScriptSelector]


class Disclosure(Contract):
    kind: Text
    text: Text


class UncertaintyNote(Contract):
    text: Text
    script_selectors: list[ScriptSelector]


class ShowNotes(Contract):
    script_ref: ArtifactRef
    transcript_ref: ArtifactRef
    audio_master_ref: ArtifactRef
    title: Text
    description: Text
    chapters: list[Chapter]
    citations: list[ShowCitation]
    disclosures: list[Disclosure]
    uncertainty_notes: list[UncertaintyNote]


class ContentCheck(Contract):
    statement: Text
    selectors: list[ShowNotesSelector]
    evidence_refs: list[ArtifactRef]
    calculation_refs: list[ArtifactRef]
    verdict: Verdict
    rationale: Text


class ShowNotesReview(Contract):
    show_notes_ref: ArtifactRef
    script_ref: ArtifactRef
    verification_report_ref: ArtifactRef
    policy_ref: ArtifactRef
    reviewed_fields: list[ShowNotesSelector]
    content_checks: list[ContentCheck]
    dependency_dispositions: list[DependencyDisposition]
    findings: list[Finding]
    recommendation: Recommendation


class FrameRange(Contract):
    start_frame: NonNegativeInt
    end_frame: NonNegativeInt
