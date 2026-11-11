"""Complete persisted reports; memory summaries are separately authored compact fields."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ..revision.scopes import ReaderAssessmentV4, SourcedFactAssessment
from ..revision.verification import AssignmentResult
from ..roles import FullText, RoleOutput, UtteranceAnchor

CompactText = Annotated[FullText, StringConstraints(max_length=1600)]
ReaderAspect = Literal[
    "story_development", "clarity", "transitions", "repetition", "ending", "voice", "idiom"
]
FactAspect = Literal["factual_accuracy", "causality", "chronology", "generalization"]


class EvidenceRef(RoleOutput):
    from_review: Literal["current", "previous"]
    evidence_id: FullText


class CoverageRange(RoleOutput):
    first_utterance_index: int = Field(ge=0)
    last_utterance_index: int = Field(ge=0)
    status: Literal["reviewed", "unchecked", "not_applicable"]
    decision_ids: list[FullText]
    explanation: FullText


class AuditArea(RoleOutput):
    status: Literal["reviewed", "unchecked", "not_applicable"]
    decision_ids: list[FullText]
    explanation: FullText


class ReaderArea(AuditArea):
    aspect: ReaderAspect


class FactArea(AuditArea):
    aspect: FactAspect


class ContextualDecision(RoleOutput):
    decision_id: str = Field(pattern=r"^D[1-9][0-9]*$")
    anchors: list[UtteranceAnchor] = Field(min_length=1)
    contextual_meaning: FullText
    context_scope: FullText
    rationale: FullText
    memory_summary: CompactText


class ClaimCheck(ContextualDecision):
    claim_types: list[Literal["factual", "causal", "chronological", "generalization"]] = Field(min_length=1)
    verdict: Literal["supported", "contradicted", "unsupported", "unchecked"]
    source_refs: list[EvidenceRef]


class ReaderJudgment(ContextualDecision):
    aspects: list[ReaderAspect] = Field(min_length=1)
    verdict: Literal["acceptable", "needs_change", "unchecked"]


class FactAudit(RoleOutput):
    coverage: list[CoverageRange] = Field(min_length=1)
    areas: list[FactArea]
    claim_checks: list[ClaimCheck]
    overall_judgment: FullText


class ReaderAudit(RoleOutput):
    coverage: list[CoverageRange] = Field(min_length=1)
    areas: list[ReaderArea]
    judgments: list[ReaderJudgment]
    overall_judgment: FullText


class FindingLink(RoleOutput):
    finding_index: int = Field(ge=0)
    decision_ids: list[FullText] = Field(min_length=1)


class DecisionDisposition(RoleOutput):
    previous_decision_id: FullText
    disposition: Literal["retained", "reassessed", "retired"]
    current_decision_ids: list[FullText]
    context_assessment: FullText
    reason: FullText


class LateFinding(RoleOutput):
    finding_index: int = Field(ge=0)
    reason_kind: Literal[
        "unresolved_assignment",
        "introduced_or_regression",
        "context_or_evidence_changed",
        "substantive_missed_issue",
    ]
    previous_decision_ids: list[FullText]
    previous_coverage_indices: list[Annotated[int, Field(ge=0)]]
    assignment_ids: list[FullText]
    current_decision_ids: list[FullText] = Field(min_length=1)
    grounds: FullText
    substantive_consequence: FullText


class AuditRelations(RoleOutput):
    finding_links: list[FindingLink]
    assignment_results: list[AssignmentResult]
    previous_dispositions: list[DecisionDisposition]
    late_findings: list[LateFinding]
    effect_on_episode: FullText | None

    @model_validator(mode="after")
    def required_tasks_stay_required(self):
        for row in self.assignment_results:
            if row.outcome == "still_open":
                index = row.followup_finding
                if index is None or index >= len(self.findings):
                    raise ValueError("audit_assignment_followup_invalid")
                if self.recommendation == "keep" or self.findings[index].priority not in (
                    "important",
                    "blocking",
                ):
                    raise ValueError("audit_open_assignment_optional")
        return self

    def verify_assignments(self, context):
        from ..revision.verification import RevisionReading

        RevisionReading.verify_assignments(self, context)


class AuditedReader(ReaderAssessmentV4, AuditRelations):
    reading_mode: Literal["script_only", "informed"]
    audit: ReaderAudit

    def anchored_groups(self):
        return {**super().anchored_groups(), "audit_judgments": self.audit.judgments}


class AuditedFact(SourcedFactAssessment, AuditRelations):
    audit: FactAudit

    def anchored_groups(self):
        return {**super().anchored_groups(), "audit_claim_checks": self.audit.claim_checks}
