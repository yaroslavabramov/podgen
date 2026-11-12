from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from podgen.domain.contracts.common import Contract, DecimalString, RuntimeRef, Text


class ReviewerProfile(Contract):
    model_id: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"
    max_output_tokens: int = Field(default=8192, ge=8192, le=32768)
    thinking_level: Literal["low", "medium", "high"] = "low"
    request_timeout_seconds: float = Field(default=180, ge=1, le=1800)


class OpenAIReviewerProfile(Contract):
    model_id: Literal["gpt-audio-1.5"] = "gpt-audio-1.5"
    max_output_tokens: int = Field(default=8192, ge=1, le=16384)
    request_timeout_seconds: float = Field(default=180, ge=1, le=1800)


class ReviewerPrice(Contract):
    usd_per_million_input_tokens: DecimalString
    usd_per_million_output_tokens: DecimalString
    source_url: Text
    checked_at: Text


class ReviewConfig(Contract):
    contract: Literal["audio-fidelity-adjudication/1"] = "audio-fidelity-adjudication/1"
    review_id: UUID
    comparison_run_id: UUID
    fidelity_audit_id: UUID
    candidate_result_ref: RuntimeRef
    reviewer: ReviewerProfile = Field(default_factory=ReviewerProfile)
    output_language: Literal["ru"] = "ru"
    credential_env: Text = "JEMINI_API_KEY"
    prompt_path: Text
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price: ReviewerPrice
    reservation_per_request_usd: DecimalString
    max_cost_usd: DecimalString
    max_requests: int = Field(default=32, ge=1, le=64)
    max_window_seconds: float = Field(default=45, ge=1, le=45)
    max_cases_per_window: int = Field(default=8, ge=1, le=16)
    case_ids: list[Text] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def limits(self):
        if not 0 < Decimal(self.reservation_per_request_usd) <= Decimal(self.max_cost_usd):
            raise ValueError("invalid_review_budget")
        if any(
            Decimal(v) < 0
            for v in (self.price.usd_per_million_input_tokens, self.price.usd_per_million_output_tokens)
        ):
            raise ValueError("negative_review_price")
        if self.case_ids is not None and len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("duplicate_review_case_id")
        return self


class OpenAIReviewerPrice(Contract):
    usd_per_million_input_text_tokens: DecimalString
    usd_per_million_input_audio_tokens: DecimalString
    usd_per_million_output_text_tokens: DecimalString
    source_url: Text
    checked_at: Text

    @model_validator(mode="after")
    def nonnegative(self):
        if any(Decimal(v) < 0 for k, v in self.model_dump().items() if k.startswith("usd_")):
            raise ValueError("negative_review_price")
        return self


class AvailabilityFallback(Contract):
    policy: Literal["gemini-to-openai-on-availability/1"]
    http_statuses: tuple[Literal[408], Literal[429]] = (408, 429)
    http_status_family: Literal["5xx"] = "5xx"
    on_outcome_unknown: Literal[True] = True
    scope: Literal["remaining_run_windows"] = "remaining_run_windows"
    max_provider_transitions: Literal[1] = 1
    reviewer: OpenAIReviewerProfile = Field(default_factory=OpenAIReviewerProfile)
    credential_env: Text = "OPENAI_API_KEY"
    price: OpenAIReviewerPrice
    reservation_per_request_usd: DecimalString


class ReviewConfigV2(ReviewConfig):
    contract: Literal["audio-fidelity-adjudication/2"] = "audio-fidelity-adjudication/2"
    fallback: AvailabilityFallback

    @model_validator(mode="after")
    def fallback_budget(self):
        if not 0 < Decimal(self.fallback.reservation_per_request_usd) <= Decimal(self.max_cost_usd):
            raise ValueError("invalid_review_fallback_budget")
        return self


def parse_config(raw):
    return TypeAdapter(ReviewConfig | ReviewConfigV2).validate_json(raw)


def validate_config(value):
    return TypeAdapter(ReviewConfig | ReviewConfigV2).validate_python(value)


def prefix(review_id):
    return "audio-review:" + str(review_id)
