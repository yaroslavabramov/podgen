from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.generation_v2.contracts.research import (
    EditorialAtomKind,
    ResearchAuditIssueType,
    ResearchAuditSeverity,
    ResearchAuditTargetStep,
    StoryWeight,
)
from app.generation_v2.contracts.source import SourceCoverageStatus
from app.generation_v2.research_kinds import normalize_research_atom_kind


class EditorialAtom(BaseModel):
    kind: EditorialAtomKind
    title: str
    # Kept separate from explanation so the research assertion survives the
    # memo -> treatment handoff. The default keeps completed legacy artifacts
    # readable; all new workspace conversions populate it explicitly.
    claim: str = ""
    explanation: str
    source_ids: list[str] = Field(default_factory=list)
    quote_refs: list[str] = Field(default_factory=list)
    story_value: str
    anchor_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    risk: str = "medium"
    source_coverage_status: SourceCoverageStatus = SourceCoverageStatus.PARTIAL
    story_weight: StoryWeight = StoryWeight.SUPPORTING
    treatment_role_hint: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, value: str) -> str:
        return normalize_research_atom_kind(str(value or ""))


class ResearchMemo(BaseModel):
    title: str
    thesis: str
    research_synthesis: str
    editorial_atoms: list[EditorialAtom]
    source_coverage: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risk_register: list[str] = Field(default_factory=list)


class ResearchAuditIssue(BaseModel):
    issue_type: ResearchAuditIssueType = ResearchAuditIssueType.OTHER
    severity: ResearchAuditSeverity = ResearchAuditSeverity.MAJOR
    target_step: ResearchAuditTargetStep = ResearchAuditTargetStep.NONE
    description: str
    suggested_repair: str = ""
    atom_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class ResearchAudit(BaseModel):
    passed: bool
    score: float = Field(ge=0, le=1)
    verdict: str
    missing_material: list[str] = Field(default_factory=list)
    required_followups: list[str] = Field(default_factory=list)
    blocking: bool = False
    issues: list[ResearchAuditIssue] = Field(default_factory=list)
    repair_targets: list[ResearchAuditTargetStep] = Field(default_factory=list)


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
    planning_gaps: list[str] = Field(default_factory=list)


class TreatmentPlanAct(BaseModel):
    act_number: int
    title: str
    story_role: str
    opening_question: str
    main_turn: str
    ending_turn: str
    source_focus: list[str] = Field(default_factory=list)
    target_minutes: float
    required_atom_refs: list[str] = Field(default_factory=list)
    source_density_target: int = 2


class TreatmentPlan(BaseModel):
    title: str
    logline: str
    host_dynamic: str
    acts: list[TreatmentPlanAct]
    recurring_questions: list[str] = Field(default_factory=list)
    style_constraints: list[str] = Field(default_factory=list)
    planning_gaps: list[str] = Field(default_factory=list)


__all__ = [
    "EditorialAtom",
    "ResearchAudit",
    "ResearchAuditIssue",
    "ResearchMemo",
    "Treatment",
    "TreatmentAct",
    "TreatmentPlan",
    "TreatmentPlanAct",
]
