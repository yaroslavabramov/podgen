from typing import Literal

from pydantic import Field, model_validator

from podgen.domain.contracts.common import Contract, Text

REASONS = dict(
    confirmed={"audible_mismatch"},
    dismissed={"asr_error", "asr_representation_conflict", "equivalent_spoken_form"},
    inconclusive={
        "unclear_speech",
        "insufficient_window",
        "audio_unavailable",
        "pronunciation_reference_unresolved",
    },
)


class HeardSpan(Contract):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    words: Text

    @model_validator(mode="after")
    def positive_span(self):
        if self.end_seconds <= self.start_seconds or not self.words.strip():
            raise ValueError("review_requires_nonempty_heard_span")
        return self


class CaseResolution(Contract):
    case_id: Text
    status: Literal["confirmed", "dismissed", "inconclusive"]
    reason_code: Literal[
        "audible_mismatch",
        "asr_error",
        "asr_representation_conflict",
        "equivalent_spoken_form",
        "unclear_speech",
        "insufficient_window",
        "audio_unavailable",
        "pronunciation_reference_unresolved",
    ]
    heard_spans: list[HeardSpan]
    explanation: Text

    @model_validator(mode="after")
    def status_basis(self):
        if self.reason_code not in REASONS[self.status]:
            raise ValueError("review_status_reason_mismatch")
        if self.status != "inconclusive" and not self.heard_spans:
            raise ValueError("review_requires_heard_evidence")
        if not self.explanation.strip():
            raise ValueError("review_requires_explanation")
        return self


class WindowResolution(Contract):
    results: list[CaseResolution]

    def validate_window(self, case_ids, duration):
        actual = [str(r.case_id) for r in self.results]
        if len(actual) != len(set(actual)) or set(actual) != set(case_ids):
            raise ValueError("review_case_set_mismatch")
        if any(s.end_seconds > duration + 1e-9 for r in self.results for s in r.heard_spans):
            raise ValueError("review_heard_span_outside_audio")
        return self
