from typing import Literal

from pydantic import JsonValue

from .common import (
    UUID,
    ArtifactRef,
    Contract,
    Finding,
    NonNegativeInt,
    RuntimeRef,
    ScriptSelector,
    SubjectSelector,
    Text,
    Timestamp,
)
from .verification import FrameRange, Verdict


class SupportedCombination(Contract):
    language: Text
    duration_seconds: NonNegativeInt
    format: Literal["solo", "dialogue"]


class EvaluationCase(Contract):
    case_family: Text
    split: Literal["development", "calibration", "holdout"]
    topic_group_id: Text
    mode: Literal["component", "episode", "failure_recovery"]
    episode_spec_ref: ArtifactRef
    input_refs: list[ArtifactRef]
    source_access_policy_ref: ArtifactRef
    reference_annotation_ref: ArtifactRef
    scoring_policy_ref: ArtifactRef
    allowed_candidate_profile_refs: list[ArtifactRef]
    supported_combination: SupportedCombination
    perturbations: list[dict[str, JsonValue]]
    prohibited_shortcuts: list[str]


class EvaluationResult(Contract):
    case_ref: ArtifactRef
    candidate_profile_refs: list[ArtifactRef]
    repeat_index: NonNegativeInt
    output_refs: list[ArtifactRef]
    run_ids: list[UUID]
    evaluator_profile_refs: list[ArtifactRef]
    measurements: list[dict[str, JsonValue]]
    human_assessment_refs: list[ArtifactRef]
    findings: list[Finding]
    outcome: Literal["pass", "fail", "inconclusive"]
    exclusions_with_reasons: list[dict[str, JsonValue]]
    started_at: Timestamp
    completed_at: Timestamp


class SourceAssertion(Contract):
    annotation_id: UUID
    statement: Text
    evidence_refs: list[ArtifactRef]
    required_qualifiers: list[str]
    importance: Literal["central", "supporting", "incidental"]


class ExpectedUnderstanding(Contract):
    question: Text
    answer_criteria: list[str]
    acceptable_alternatives: list[str]
    basis_refs: list[ArtifactRef]


class ForbiddenInference(Contract):
    statement: Text
    reason: Text
    basis_refs: list[ArtifactRef]


class KnownUncertainty(Contract):
    description: Text
    basis_refs: list[ArtifactRef]


class EvaluationAnnotation(Contract):
    episode_spec_ref: ArtifactRef
    source_snapshot_refs: list[ArtifactRef]
    source_assertions: list[SourceAssertion]
    expected_understanding: list[ExpectedUnderstanding]
    forbidden_inferences: list[ForbiddenInference]
    known_uncertainties: list[KnownUncertainty]
    scoring_policy_ref: ArtifactRef
    annotator_assignment_ids: list[UUID]


class AssessmentAssignment(Contract):
    assignment_id: UUID
    anonymous_rater_id: Text
    role: Literal["factchecker", "editor", "listener"]
    blind_candidate_ids: list[str]
    presentation_order: list[str]
    task: Literal[
        "source_audit", "editorial_review", "full_comprehension", "voluntary_interest", "pairwise_preference"
    ]


class Rating(Contract):
    dimension: Text
    score: int | None
    abstain_reason: str | None
    rationale: Text
    evidence_selectors: list[SubjectSelector]


class HumanFactCheck(Contract):
    statement: Text
    location: SubjectSelector
    verdict: Verdict
    evidence_refs: list[ArtifactRef]
    rationale: Text


class Comprehension(Contract):
    free_answer: str
    stated_limits: list[str]
    transfer_answer: str


class MasterSubject(Contract):
    kind: Literal["master"]
    audio_master_ref: ArtifactRef


class ReadingSubject(Contract):
    kind: Literal["evaluation_reading"]
    recording_ref: RuntimeRef
    script_ref: ArtifactRef
    spoken_view_hash: Text


class OmittedRange(FrameRange):
    reason: Text


class HumanAudioCoverage(Contract):
    subject: MasterSubject | ReadingSubject
    sample_rate_hz: NonNegativeInt
    sample_frames: NonNegativeInt
    listened_ranges: list[FrameRange]
    completion: Literal["full", "partial"]
    omitted_ranges_with_reasons: list[OmittedRange]


class VoluntaryObservation(Contract):
    outcome: Literal[
        "not_started",
        "completed",
        "stopped_content",
        "stopped_external",
        "technical_failure",
        "observation_end",
        "lost_followup",
        "unknown_reason",
    ]
    last_frame: NonNegativeInt | None
    location: ScriptSelector | None
    stated_reason: str | None
    prior_topic_interest: str | None
    prior_familiarity: str | None


class PairwisePreference(Contract):
    winner_blind_id: str | None
    outcome: Literal["preferred", "tie", "abstain"]
    rationale: Text


class HumanAssessment(Contract):
    case_ref: ArtifactRef
    evaluated_output_refs: list[ArtifactRef]
    rubric_ref: ArtifactRef
    assignment: AssessmentAssignment
    ratings: list[Rating]
    fact_checks: list[HumanFactCheck]
    comprehension: Comprehension | None
    audio_coverage: HumanAudioCoverage | None
    voluntary_observation: VoluntaryObservation | None
    pairwise_preference: PairwisePreference | None
    completed_at: Timestamp
