from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue

from podgen.domain.contracts.common import BlobRef, Contract


class ReviewRange(Contract):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    basis: Literal["matched_segment_timestamps", "neighbor_anchors_or_full_audio_edges"]
    margin_frames: int = Field(ge=0)
    boundary_precision: Literal["recognizer_hypothesis"]


class Candidate(Contract):
    candidate_id: UUID
    kind: Literal["script_discrepancy", "recognition_uncertainty"]
    recognized_token_range: list[int] = Field(min_length=2, max_length=2)
    script_selectors: list[dict[str, JsonValue]]
    audio_range: ReviewRange
    status: Literal["pending_audio_adjudication", "inconclusive"]
    clip_status: Literal["ready_for_extraction", "extracted", "range_exceeds_review_limit"]
    conclusion: Literal["candidate_only_not_confirmed_tts_error"]
    operation: Literal["replace", "insert", "delete"] | None = None
    expected_token_range: list[int] | None = None
    expected_tokens: list[dict[str, JsonValue]] | None = None
    recognized_tokens: list[dict[str, JsonValue]] | None = None
    expected_context: list[dict[str, JsonValue]] | None = None
    issue: dict[str, JsonValue] | None = None
    timing_uncertainties: list[dict[str, JsonValue]] = Field(default_factory=list)
    clip_blob: BlobRef | None = None
    extraction: dict[str, JsonValue] | None = None


class CandidateReport(Contract):
    contract: Literal[
        "literal-fidelity-candidates/1",
        "literal-fidelity-candidates/2",
        "literal-fidelity-candidates/3",
        "literal-fidelity-candidates/4",
    ]
    normalization_contract: Literal[
        "literal-ru-case-punctuation-yo/1",
        "literal-ru-case-punctuation-yo/2",
        "literal-ru-case-punctuation-yo/3",
    ]
    normalization_equivalences: list[dict[str, JsonValue]] = Field(default_factory=list)
    expected_tokens: list[dict[str, JsonValue]]
    recognized_tokens: list[dict[str, JsonValue]]
    candidates: list[Candidate]
    recognition_uncertainties: list[dict[str, JsonValue]]
    localization_status: Literal["uncertain", "mapped"] = "uncertain"
    edit_distance_diagnostic: int = Field(ge=0)
    expected_token_count: int = Field(ge=0)
    audio_review_status: Literal["pending"]
    production_ready: Literal[False]
    decision: Literal["candidates_generated"]


class AlignmentProvenance(Contract):
    contract: Literal["kaldialign-text-alignment/1"]
    engine: Literal["kaldialign"]
    engine_version: Literal["0.12.0"]
    merge_compounds: Literal["alphabetic_literal_units_only"]
    sclite_mode: Literal[False]
    integer_rule: Literal["literal-ru-integer-surface-equivalence/1"]
    compound_rule: Literal["literal-alphabetic-compound-equivalence/1"]


class CandidateReportV5(CandidateReport):
    contract: Literal["literal-fidelity-candidates/5"]
    alignment: AlignmentProvenance


def validate_report(value):
    model = CandidateReportV5 if value.get("contract") == "literal-fidelity-candidates/5" else CandidateReport
    return model.model_validate(value)
