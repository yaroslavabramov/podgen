from typing import Literal

from .common import (
    UUID,
    ArtifactRef,
    Contract,
    DependencyDisposition,
    NoteTextRange,
    SubjectSelector,
    Text,
    Timestamp,
)
from .episodes import Temporal


class AlternativeExplanation(Contract):
    local_id: UUID
    statement: Text
    distinguishing_observations: list[str]
    support_refs: list[ArtifactRef]
    counter_refs: list[ArtifactRef]
    state: Literal["open", "supported", "weakened", "ruled_out"]
    rationale: str


class EditorialState(Contract):
    episode_spec_ref: ArtifactRef
    dossier_ref: ArtifactRef | None
    main_question: Text
    working_answer: Text | None
    answer_basis_refs: list[ArtifactRef]
    alternatives: list[AlternativeExplanation]
    listener_promise: Text | None
    active_question_refs: list[ArtifactRef]
    story_plan_ref: ArtifactRef | None
    script_ref: ArtifactRef | None
    current_decision_refs: list[ArtifactRef]
    phase: Literal[
        "orienting", "researching", "planning", "drafting", "editing", "verifying", "producing_audio", "ready"
    ]


class QuestionScope(Contract):
    subject: Text
    geography: str
    temporal: Temporal


class QuestionRecord(Contract):
    question: Text
    why_it_matters: Text
    scope: QuestionScope
    origin_ref: ArtifactRef
    acceptance_questions: list[str]
    known_note_refs: list[ArtifactRef]
    competing_explanations: list[str]
    impact: Literal["central_answer", "local_passage", "enrichment"]
    resolution: Literal["open", "answered", "partial", "deferred", "unanswerable"]
    answer_note_refs: list[ArtifactRef]
    uncertainty_refs: list[ArtifactRef]
    resolution_reason: str | None


class NoteCitation(Contract):
    marker: Text
    evidence_refs: list[ArtifactRef]
    relation: Literal["supports", "qualifies", "contradicts"]
    explanation: str | None


class InterpretationBinding(Contract):
    text_selector: NoteTextRange
    basis_markers: list[str]
    kind: Literal["synthesis", "causal_hypothesis", "analogy", "calculation"]
    rationale: Text
    limitations: list[str]


class ResearchNote(Contract):
    question_refs: list[ArtifactRef]
    title: Text
    answer_markdown: Text
    citations: list[NoteCitation]
    interpretations: list[InterpretationBinding]
    limitations: list[str]
    uncertainty_refs: list[ArtifactRef]
    supersedes_note_refs: list[ArtifactRef]


class UncertaintyRecord(Contract):
    question_ref: ArtifactRef
    description: Text
    kind: Literal[
        "acquisition_blocked", "corpus_gap", "research_incomplete", "conflicting_evidence", "world_unknown"
    ]
    basis_refs: list[ArtifactRef]
    search_attempt_ids: list[UUID]
    affected_statement: Text
    impact: Literal["central_answer", "local_passage", "enrichment"]
    handling: Literal["investigate", "qualify", "omit", "explicit_unknown", "defer"]
    next_action: str | None
    resolution_note_ref: ArtifactRef | None


class CandidateMaterial(Contract):
    note_ref: ArtifactRef
    text_selector: NoteTextRange | None
    use: Literal["mechanism", "scene", "example", "counterexample", "quote"]


class ExcludedTopic(Contract):
    topic: Text
    reason: Text
    decision_ref: ArtifactRef


class DossierSnapshot(Contract):
    episode_spec_ref: ArtifactRef
    knowledge_as_of: Timestamp
    source_policy_ref: ArtifactRef
    overview_note_ref: ArtifactRef
    timeline_note_ref: ArtifactRef | None
    note_refs: list[ArtifactRef]
    question_refs: list[ArtifactRef]
    uncertainty_refs: list[ArtifactRef]
    source_revision_refs: list[ArtifactRef]
    evidence_refs: list[ArtifactRef]
    candidate_materials: list[CandidateMaterial]
    excluded_topics: list[ExcludedTopic]


class ConsideredOption(Contract):
    option: Text
    benefit: str
    unresolved_problem: str
    basis_refs: list[ArtifactRef]


class SubjectDispositions(Contract):
    subject_ref: ArtifactRef
    dispositions: list[DependencyDisposition]


class RequestDisposition(Contract):
    request_ref: ArtifactRef
    action: Literal["still_open", "addressed", "obsolete"]
    current_subject_ref: ArtifactRef
    current_location: SubjectSelector | None
    basis_refs: list[ArtifactRef]
    reason: Text


class EditorialDecision(Contract):
    state_ref: ArtifactRef
    action: Literal[
        "commission", "integrate", "change_direction", "proceed", "revise", "narrow_scope", "stop"
    ]
    reason: Text
    considered_options: list[ConsideredOption]
    selected_option: Text
    input_result_ids: list[UUID]
    question_refs: list[ArtifactRef]
    reviewed_output_refs: list[ArtifactRef]
    dependency_dispositions: list[SubjectDispositions]
    request_dispositions: list[RequestDisposition]
    expected_effect: Text
    remaining_risks: list[str]


class ResearchRequest(Contract):
    requester_ref: ArtifactRef
    location_selector: SubjectSelector | None
    question: Text
    why_it_matters: Text
    current_understanding: str
    context_refs: list[ArtifactRef]
    acceptance_questions: list[str]
    impact: Literal["central_answer", "local_passage", "enrichment"]
    blocking: Literal["none", "passage", "central_answer"]
    requested_outcome: Literal["explanation", "support", "contradiction", "example"]
