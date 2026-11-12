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
    clip_blob: BlobRef | None = None
    extraction: dict[str, JsonValue] | None = None


class CandidateReport(Contract):
    contract: Literal["literal-fidelity-candidates/1"]
    normalization_contract: Literal["literal-ru-case-punctuation-yo/1"]
    expected_tokens: list[dict[str, JsonValue]]
    recognized_tokens: list[dict[str, JsonValue]]
    candidates: list[Candidate]
    recognition_uncertainties: list[dict[str, JsonValue]]
    edit_distance_diagnostic: int = Field(ge=0)
    expected_token_count: int = Field(ge=0)
    audio_review_status: Literal["pending"]
    production_ready: Literal[False]
    decision: Literal["candidates_generated"]
