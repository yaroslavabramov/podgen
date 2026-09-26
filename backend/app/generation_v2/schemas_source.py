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
    answer_requirements: list[CoverageRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_answer_requirements(self) -> "CoverageQuestion":
        _unique_ids(self.answer_requirements, "requirement_id", "coverage requirement")
        return self


class CoverageRequirement(BaseModel):
    """One freely worded part that a complete answer to a question must resolve."""

    requirement_id: str
    description: str
    rationale: str = ""
    completion_criterion: str = ""
    synthesis_only: bool = False
    delegated_root_requirement_ids: list[str] = Field(default_factory=list)


class CoverageQuestionAssignment(BaseModel):
    """Planner-owned responsibility for checking question parts in one source set."""

    question_id: str
    requirement_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_requirement_ids(self) -> "CoverageQuestionAssignment":
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("coverage question assignment requirement_ids must be unique")
        return self


class FindingQuestionLink(BaseModel):
    question_id: str
    requirement_ids: list[str] = Field(default_factory=list)
    rationale: str = ""

    @model_validator(mode="after")
    def validate_requirement_ids(self) -> "FindingQuestionLink":
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("finding question link requirement_ids must be unique")
        return self


class FindingScope(BaseModel):
    actor: str = ""
    period: str = ""
    geography: str = ""
    qualifiers: list[str] = Field(default_factory=list)


class SourceReadingBasis(BaseModel):
    receipt_refs: list[str] = Field(default_factory=list)
    coverage_summary: str = ""
    unread_regions: str = ""
    unread_risk_assessment: str = ""
    representation_limits: str = ""
    stopping_reason: str = ""
    coverage_ledger: dict = Field(default_factory=dict)


class SourceQuestionDisposition(BaseModel):
    question_id: str
    status: str
    rationale: str = ""


class FindingSourceRelation(BaseModel):
    related_source_id: str
    relationship: SourceRelationshipKind
    rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class SourceFinding(BaseModel):
    """Durable source-level meaning; evidence text remains in the evidence store."""

    finding_id: str
    source_id: str
    source_version_ids: list[str] = Field(default_factory=list)
    assessment_id: str | None = None
    statement: str
    support_explanation: str = ""
    evidence_ids: list[str] = Field(min_length=1)
    scope: FindingScope = Field(default_factory=FindingScope)
    limitations: list[str] = Field(default_factory=list)
    possible_question_ids: list[str] = Field(default_factory=list)
    question_links: list[FindingQuestionLink] = Field(default_factory=list)
    source_relations: list[FindingSourceRelation] = Field(default_factory=list)
    semantic_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_question_links(self) -> "SourceFinding":
        question_ids = [item.question_id for item in self.question_links]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("finding question links must be unique per question_id")
        return self


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
    finding_ids: list[str] = Field(default_factory=list)
    reading_basis: SourceReadingBasis = Field(default_factory=SourceReadingBasis)
    question_dispositions: list[SourceQuestionDisposition] = Field(default_factory=list)
    no_findings_reason: str = ""
    limitations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class SourceAnalysisReadState(BaseModel):
    complete: bool = True
    unread_regions: list[str] = Field(default_factory=list)
    stopping_reason: str = ""
    backend_complete: bool = False
    backend_eof: bool = False
    backend_representation_failure: bool = False

    @model_validator(mode="after")
    def validate_incomplete_state(self) -> "SourceAnalysisReadState":
        if not self.complete and (not self.unread_regions or not self.stopping_reason.strip()):
            raise ValueError("incomplete source analysis read_state requires unread_regions and stopping_reason")
        return self


class SourceAnalysisFinding(BaseModel):
    finding_id: str = ""
    statement: str
    evidence_ids: list[str] = Field(default_factory=list, min_length=1)
    question_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_mapping_finding(self) -> "SourceAnalysisFinding":
        forbidden = {"covered", "partial", "missing"}
        text_values = [self.statement, *self.limitations]
        if any(str(value).strip().lower() in forbidden for value in text_values):
            raise ValueError("source analysis findings cannot contain coverage status labels")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("source analysis finding evidence_ids must be unique")
        if len(self.question_ids) != len(set(self.question_ids)):
            raise ValueError("source analysis finding question_ids must be unique")
        return self


class SourceAnalysisGap(BaseModel):
    question_id: str
    requirement_id: str = ""
    description: str

    @model_validator(mode="after")
    def validate_source_mapping_gap(self) -> "SourceAnalysisGap":
        forbidden = {"covered", "partial", "missing"}
        if str(self.description).strip().lower() in forbidden:
            raise ValueError("source analysis gaps cannot contain coverage status labels")
        return self


class SourceAnalysis(BaseModel):
    """Durable one-source mapping record. It is not a question answer."""

    analysis_id: str
    source_ref: str
    source_id: str = ""
    source_version_ids: list[str] = Field(default_factory=list)
    set_id: str = ""
    status: str = "usable"
    status_reason: str = ""
    summary: str = ""
    source_limitations: list[str] = Field(default_factory=list)
    read_state: SourceAnalysisReadState = Field(default_factory=SourceAnalysisReadState)
    findings: list[SourceAnalysisFinding] = Field(default_factory=list)
    gaps: list[SourceAnalysisGap] = Field(default_factory=list)
    duplicate_of_source_ref: str = ""
    current: bool = True
    accepted: bool = True
    attempt: int = Field(default=1, ge=1)
    created_at: str = ""
    updated_at: str = ""

    @model_validator(mode="after")
    def validate_source_mapping_analysis(self) -> "SourceAnalysis":
        allowed_statuses = {"usable", "deferred", "not_target_content"}
        if self.status not in allowed_statuses:
            raise ValueError("source analysis status must be usable, deferred, or not_target_content")
        if self.status != "usable" and not self.status_reason.strip():
            raise ValueError("deferred/not_target_content source analysis requires status_reason")
        if self.status == "usable" and not self.summary.strip():
            raise ValueError("usable source analysis requires summary")
        if self.status in {"deferred", "not_target_content"} and self.findings:
            raise ValueError("deferred/not_target_content source analysis cannot contain findings")
        finding_ids = [item.finding_id for item in self.findings if item.finding_id]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("source analysis finding_ids must be unique")
        return self


class CoverageRequirementResult(BaseModel):
    requirement_id: str
    status: CoverageOutcomeStatus = CoverageOutcomeStatus.UNKNOWN
    finding_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    closure_basis: str = ""
    remaining_uncertainty: str = ""
    missing_detail: str = ""
    conflict_summary: str = ""
    deferred_reason: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    next_action: CoverageNextAction = CoverageNextAction.RESEARCH

    @model_validator(mode="after")
    def validate_finding_ids(self) -> "CoverageRequirementResult":
        if len(self.finding_ids) != len(set(self.finding_ids)):
            raise ValueError("coverage requirement result finding_ids must be unique")
        return self


class CoverageOutcome(BaseModel):
    outcome_id: str
    question_id: str
    status: CoverageOutcomeStatus = CoverageOutcomeStatus.UNKNOWN
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    requirement_results: list[CoverageRequirementResult] = Field(default_factory=list)
    orientation_summary: str = ""
    missing_detail: str = ""
    conflict_summary: str = ""
    deferred_reason: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    next_action: CoverageNextAction = CoverageNextAction.RESEARCH
    semantic_revision: int = Field(default=0, ge=0)
    is_stale: bool = False

    @model_validator(mode="after")
    def validate_requirement_results(self) -> "CoverageOutcome":
        _unique_ids(self.requirement_results, "requirement_id", "coverage requirement result")
        if len(self.finding_ids) != len(set(self.finding_ids)):
            raise ValueError("coverage outcome finding_ids must be unique")
        return self


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
    question_assignments: list[CoverageQuestionAssignment] = Field(default_factory=list)
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
    question_assignments: list[CoverageQuestionAssignment] = Field(default_factory=list)
    question: str = ""
    requirements: list[CoverageRequirement] = Field(default_factory=list)
    findings: list[dict] = Field(default_factory=list)
    gaps: list[dict] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    source_cards: list[dict] = Field(default_factory=list)
    root_only: bool = False
    synthesis_only: bool = False
    # Legacy/read compatibility only. New source-mapping handoff does not write these.
    finding_ids: list[str] = Field(default_factory=list)
    final_finding_ids: list[str] = Field(default_factory=list)
    candidate_finding_ids: list[str] = Field(default_factory=list)
    allowed_source_ids: list[str] = Field(default_factory=list)
    preferred_source_ids: list[str] = Field(default_factory=list)
    candidate_source_ids: list[str] = Field(default_factory=list)
    starting_evidence_ids: list[str] = Field(default_factory=list)
    candidate_evidence_ids: list[str] = Field(default_factory=list)
    coverage_hint_ids: list[str] = Field(default_factory=list)
    action: ResearchHandoffAction = ResearchHandoffAction.NONE
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
    source_findings: list[SourceFinding] = Field(default_factory=list)
    source_analyses: list[SourceAnalysis] = Field(default_factory=list)
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
        finding_ids = _unique_ids(self.source_findings, "finding_id", "source finding")
        _unique_ids(self.source_analyses, "analysis_id", "source analysis")
        _validate_structured_outcome_finding_union(
            self.coverage_questions,
            self.coverage_outcomes,
        )
        _derive_outcome_finding_lineage(self.source_findings, self.coverage_outcomes)
        _unique_ids(self.research_handoff, "handoff_id", "research handoff item")
        _unique_ids(self.gap_records, "gap_id", "gap record")
        outcome_question_ids = [item.question_id for item in self.coverage_outcomes]
        if len(outcome_question_ids) != len(set(outcome_question_ids)):
            raise ValueError("coverage outcomes must be unique per question_id")
        referenced_question_ids = {
            *(question_id for item in self.source_sets for question_id in item.question_ids),
            *(
                assignment.question_id
                for item in self.source_sets
                for assignment in item.question_assignments
            ),
            *(
                link.question_id
                for finding in self.source_findings
                for link in finding.question_links
            ),
            *(item.question_id for item in self.coverage_outcomes),
            *(question_id for item in self.research_handoff for question_id in item.question_ids),
            *(
                assignment.question_id
                for item in self.research_handoff
                for assignment in item.question_assignments
            ),
            *(question_id for item in self.gap_records for question_id in item.question_ids),
        }
        unknown = sorted(referenced_question_ids - question_ids)
        if unknown:
            raise ValueError(f"unknown coverage question ids: {', '.join(unknown)}")
        for question in self.coverage_questions:
            if question.parent_question_id and question.parent_question_id not in question_ids:
                raise ValueError(f"unknown parent coverage question id: {question.parent_question_id}")
        requirements_by_question = {
            question.question_id: {
                requirement.requirement_id for requirement in question.answer_requirements
            }
            for question in self.coverage_questions
        }
        for source_set in self.source_sets:
            assignment_question_ids = [item.question_id for item in source_set.question_assignments]
            if len(assignment_question_ids) != len(set(assignment_question_ids)):
                raise ValueError("source set question assignments must be unique per question_id")
            if not set(assignment_question_ids).issubset(set(source_set.question_ids)):
                raise ValueError(
                    "source set question_assignments must reference question_ids in the same set"
                )
            for assignment in source_set.question_assignments:
                unknown_requirements = sorted(
                    set(assignment.requirement_ids)
                    - requirements_by_question.get(assignment.question_id, set())
                )
                if unknown_requirements:
                    raise ValueError(
                        "unknown coverage requirement ids for "
                        f"{assignment.question_id}: {', '.join(unknown_requirements)}"
                    )
        for item in self.research_handoff:
            assignment_question_ids = [
                assignment.question_id for assignment in item.question_assignments
            ]
            if len(assignment_question_ids) != len(set(assignment_question_ids)):
                raise ValueError("research handoff assignments must be unique per question_id")
            if not set(assignment_question_ids).issubset(set(item.question_ids)):
                raise ValueError(
                    "research handoff assignments must reference question_ids in the same item"
                )
            for assignment in item.question_assignments:
                unknown_requirements = sorted(
                    set(assignment.requirement_ids)
                    - requirements_by_question.get(assignment.question_id, set())
                )
                if unknown_requirements:
                    raise ValueError(
                        "unknown coverage requirement ids for "
                        f"{assignment.question_id}: {', '.join(unknown_requirements)}"
                    )
        finding_links: dict[str, set[str]] = {}
        finding_requirement_links: dict[str, dict[str, set[str]]] = {}
        finding_possible_links: dict[str, set[str]] = {}
        for finding in self.source_findings:
            linked_questions: set[str] = set()
            linked_requirements: dict[str, set[str]] = {}
            for link in finding.question_links:
                linked_questions.add(link.question_id)
                linked_requirements[link.question_id] = set(link.requirement_ids)
                unknown_requirements = sorted(
                    set(link.requirement_ids)
                    - requirements_by_question.get(link.question_id, set())
                )
                if unknown_requirements:
                    raise ValueError(
                        "unknown coverage requirement ids for "
                        f"{link.question_id}: {', '.join(unknown_requirements)}"
                    )
            finding_links[finding.finding_id] = linked_questions
            finding_requirement_links[finding.finding_id] = linked_requirements
            finding_possible_links[finding.finding_id] = set(finding.possible_question_ids)
        for outcome in self.coverage_outcomes:
            unknown_findings = sorted(set(outcome.finding_ids) - finding_ids)
            if unknown_findings:
                raise ValueError(f"unknown source finding ids: {', '.join(unknown_findings)}")
            unlinked_findings = sorted(
                finding_id
                for finding_id in outcome.finding_ids
                if outcome.question_id not in finding_links.get(finding_id, set())
            )
            if unlinked_findings:
                raise ValueError(
                    "coverage outcome uses findings not linked to its question: "
                    + ", ".join(unlinked_findings)
                )
            requirement_ids = requirements_by_question.get(outcome.question_id, set())
            unknown_results = sorted(
                {item.requirement_id for item in outcome.requirement_results} - requirement_ids
            )
            if unknown_results:
                raise ValueError(
                    "unknown coverage requirement result ids for "
                    f"{outcome.question_id}: {', '.join(unknown_results)}"
                )
            for result in outcome.requirement_results:
                unknown_result_findings = sorted(set(result.finding_ids) - finding_ids)
                if unknown_result_findings:
                    raise ValueError(
                        "unknown source finding ids: " + ", ".join(unknown_result_findings)
                    )
                invalid_result_findings = sorted(
                    finding_id
                    for finding_id in result.finding_ids
                    if result.requirement_id
                    not in finding_requirement_links.get(finding_id, {}).get(
                        outcome.question_id, set()
                    )
                )
                if invalid_result_findings:
                    raise ValueError(
                        "coverage requirement result uses findings not linked to its requirement: "
                        + ", ".join(invalid_result_findings)
                    )
        for item in self.research_handoff:
            unknown_findings = sorted(
                set(item.finding_ids + item.final_finding_ids + item.candidate_finding_ids)
                - finding_ids
            )
            if unknown_findings:
                raise ValueError(f"unknown source finding ids: {', '.join(unknown_findings)}")
            if not set(item.final_finding_ids).issubset(set(item.finding_ids)):
                raise ValueError("research handoff final_finding_ids must be included in finding_ids")
            if not set(item.candidate_finding_ids).issubset(set(item.finding_ids)):
                raise ValueError("research handoff candidate_finding_ids must be included in finding_ids")
            if item.allowed_source_ids:
                allowed_source_ids = set(item.allowed_source_ids)
                if not set(item.preferred_source_ids).issubset(allowed_source_ids):
                    raise ValueError("research handoff preferred_source_ids must be included in allowed_source_ids")
                if not set(item.candidate_source_ids).issubset(allowed_source_ids):
                    raise ValueError("research handoff candidate_source_ids must be included in allowed_source_ids")
            unlinked_findings = sorted(
                finding_id
                for finding_id in item.finding_ids
                if not finding_links.get(finding_id, set()).intersection(item.question_ids)
                and not finding_possible_links.get(finding_id, set()).intersection(item.question_ids)
                and finding_id not in set(item.candidate_finding_ids)
            )
            if unlinked_findings:
                raise ValueError(
                    "research handoff uses findings without final or candidate question linkage: "
                    + ", ".join(unlinked_findings)
                )
            unknown_embedded_evidence_ids = sorted(
                {
                    str(evidence_id)
                    for finding in item.findings
                    if isinstance(finding, dict)
                    for evidence_id in finding.get("evidence_ids") or []
                    if str(evidence_id)
                }
                - set(item.evidence_ids)
            )
            if unknown_embedded_evidence_ids:
                raise ValueError(
                    "research handoff embedded findings must use evidence_ids listed on the handoff: "
                    + ", ".join(unknown_embedded_evidence_ids)
                )
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


def _validate_source_finding_lineage(
    questions: list[CoverageQuestion],
    findings: list[SourceFinding],
    outcomes: list[CoverageOutcome],
) -> None:
    _validate_structured_outcome_finding_union(questions, outcomes)
    question_ids = {item.question_id for item in questions}
    requirements_by_question = {
        question.question_id: {
            requirement.requirement_id for requirement in question.answer_requirements
        }
        for question in questions
    }
    finding_ids = {item.finding_id for item in findings}
    _derive_outcome_finding_lineage(findings, outcomes)
    requirement_links: dict[str, dict[str, set[str]]] = {}
    for finding in findings:
        links: dict[str, set[str]] = {}
        for link in finding.question_links:
            if link.question_id not in question_ids:
                raise ValueError(f"unknown coverage question ids: {link.question_id}")
            unknown_requirements = set(link.requirement_ids) - requirements_by_question.get(
                link.question_id, set()
            )
            if unknown_requirements:
                raise ValueError(
                    "unknown coverage requirement ids for "
                    f"{link.question_id}: {', '.join(sorted(unknown_requirements))}"
                )
            links[link.question_id] = set(link.requirement_ids)
        requirement_links[finding.finding_id] = links
    for outcome in outcomes:
        if outcome.question_id not in question_ids:
            raise ValueError(f"unknown coverage question ids: {outcome.question_id}")
        for finding_id in outcome.finding_ids:
            if finding_id not in finding_ids:
                raise ValueError(f"unknown source finding ids: {finding_id}")
            if outcome.question_id not in requirement_links.get(finding_id, {}):
                raise ValueError(
                    "coverage outcome uses findings not linked to its question: " + finding_id
                )
        for result in outcome.requirement_results:
            if result.requirement_id not in requirements_by_question.get(outcome.question_id, set()):
                raise ValueError(
                    "unknown coverage requirement result ids for "
                    f"{outcome.question_id}: {result.requirement_id}"
                )
            for finding_id in result.finding_ids:
                if finding_id not in finding_ids:
                    raise ValueError(f"unknown source finding ids: {finding_id}")
                if result.requirement_id not in requirement_links.get(finding_id, {}).get(
                    outcome.question_id, set()
                ):
                    raise ValueError(
                        "coverage requirement result uses findings not linked to its requirement: "
                        + finding_id
                    )


def _validate_structured_outcome_finding_union(
    questions: list[CoverageQuestion],
    outcomes: list[CoverageOutcome],
) -> None:
    structured_question_ids = {
        question.question_id
        for question in questions
        if question.answer_requirements
    }
    for outcome in outcomes:
        if outcome.question_id not in structured_question_ids:
            continue
        result_finding_ids = {
            finding_id
            for result in outcome.requirement_results
            for finding_id in result.finding_ids
        }
        if outcome.finding_ids and set(outcome.finding_ids) != result_finding_ids:
            raise ValueError(
                "structured coverage outcome finding_ids must exactly match requirement result findings"
            )


def _derive_outcome_finding_lineage(
    findings: list[SourceFinding], outcomes: list[CoverageOutcome]
) -> None:
    finding_by_id = {item.finding_id: item for item in findings}
    for outcome in outcomes:
        referenced_ids = list(
            dict.fromkeys(
                [
                    *outcome.finding_ids,
                    *[
                        finding_id
                        for result in outcome.requirement_results
                        for finding_id in result.finding_ids
                    ],
                ]
            )
        )
        if not referenced_ids:
            continue
        referenced_findings = [
            finding_by_id[finding_id]
            for finding_id in referenced_ids
            if finding_id in finding_by_id
        ]
        outcome.finding_ids = referenced_ids
        outcome.source_ids = list(
            dict.fromkeys(finding.source_id for finding in referenced_findings)
        )
        outcome.evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for finding in referenced_findings
                for evidence_id in finding.evidence_ids
            )
        )


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
    source_analyses: list[SourceAnalysis] = Field(default_factory=list)
    coverage_questions: list[CoverageQuestion] = Field(default_factory=list)
    source_findings: list[SourceFinding] = Field(default_factory=list)
    coverage_outcomes: list[CoverageOutcome] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_gap_records(self) -> "SourceMap":
        _unique_ids(self.gap_records, "gap_id", "gap record")
        _unique_ids(self.coverage_questions, "question_id", "coverage question")
        _unique_ids(self.source_analyses, "analysis_id", "source analysis")
        finding_ids = _unique_ids(self.source_findings, "finding_id", "source finding")
        _unique_ids(self.coverage_outcomes, "outcome_id", "coverage outcome")
        finding_by_id = {item.finding_id: item for item in self.source_findings}
        if self.source_assessments:
            assessment_by_id = {
                item.assessment_id: item for item in self.source_assessments
            }
            for finding in self.source_findings:
                if finding.assessment_id and finding.assessment_id not in assessment_by_id:
                    raise ValueError(f"unknown source assessment id: {finding.assessment_id}")
                if (
                    finding.assessment_id
                    and assessment_by_id[finding.assessment_id].source_id != finding.source_id
                ):
                    raise ValueError(
                        f"source finding {finding.finding_id} does not belong to its assessment"
                    )
            for assessment in self.source_assessments:
                for finding_id in assessment.finding_ids:
                    if finding_id not in finding_by_id:
                        raise ValueError(f"unknown source finding ids: {finding_id}")
                    if finding_by_id[finding_id].source_id != assessment.source_id:
                        raise ValueError(
                            f"source finding {finding_id} does not belong to its assessment"
                        )
        unknown_findings = sorted(
            {
                finding_id
                for outcome in self.coverage_outcomes
                for finding_id in outcome.finding_ids
            }
            - finding_ids
        )
        if unknown_findings:
            raise ValueError(f"unknown source finding ids: {', '.join(unknown_findings)}")
        _validate_source_finding_lineage(
            self.coverage_questions,
            self.source_findings,
            self.coverage_outcomes,
        )
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
    "CoverageQuestionAssignment",
    "CoverageOutcome",
    "CoverageQuestion",
    "CoverageRequirement",
    "CoverageRequirementResult",
    "FindingQuestionLink",
    "FindingScope",
    "FindingSourceRelation",
    "QuoteCard",
    "ResearchHandoffItem",
    "ResearchWorkflowCompletion",
    "SourceAssessment",
    "SourceAnalysis",
    "SourceAnalysisFinding",
    "SourceAnalysisGap",
    "SourceAnalysisReadState",
    "SourceCard",
    "SourceCluster",
    "SourceGapRecord",
    "SourceMap",
    "SourceFinding",
    "SourceRelationship",
    "SourceTriageCompletion",
    "SourceTriageGate",
    "SourceTriageItem",
    "SourceTriageReport",
    "SourceProfile",
]
