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
    words: Text = Field(
        description=(
            "Only words actually heard in this audio range. Do not copy unheard script or ASR words, "
            "and do not use diagnostic labels or placeholders as heard speech."
        )
    )

    @model_validator(mode="after")
    def positive_span(self):
        if self.end_seconds <= self.start_seconds or not self.words.strip():
            raise ValueError("review_requires_nonempty_heard_span")
        return self


class CaseResolution(Contract):
    case_id: Text
    status: Literal["confirmed", "dismissed", "inconclusive"] = Field(
        description=(
            "Verdict about actual audio versus the intended script in expected_words and expected_context. "
            "confirmed means an audible difference from that script; dismissed means the audio matches "
            "the intended spoken words, including equivalent spoken forms, regardless of ASR differences; "
            "inconclusive means this comparison cannot be "
            "established from the available audio evidence."
        )
    )
    reason_code: Literal[
        "audible_mismatch",
        "asr_error",
        "asr_representation_conflict",
        "equivalent_spoken_form",
        "unclear_speech",
        "insufficient_window",
        "audio_unavailable",
        "pronunciation_reference_unresolved",
    ] = Field(
        description=(
            "Reason for the audio-versus-script verdict. audible_mismatch requires an audible difference "
            "from the intended script, not merely from ASR. When audio matches the script, dismiss the "
            "case even if ASR differs, using the applicable dismissed reason."
        )
    )
    heard_spans: list[HeardSpan] = Field(
        description=(
            "Words actually heard, with ranges relative to the supplied audio window, supporting the "
            "audio-versus-script verdict. Never put diagnostic placeholders in these spans. "
            "Use an empty list when inconclusive and no words can actually be heard."
        )
    )
    explanation: Text = Field(
        description=(
            "Explain the verdict by comparing the actual heard words with expected_words and the intended "
            "script in expected_context, or state why that comparison remains unresolved. An ASR-versus-audio "
            "difference alone does not establish a script-reading defect."
        )
    )

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
