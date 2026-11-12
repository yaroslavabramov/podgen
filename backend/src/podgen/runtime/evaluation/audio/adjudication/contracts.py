from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from podgen.domain.contracts.common import Contract, DecimalString, RuntimeRef, Text


class ReviewerProfile(Contract):
    model_id: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"
    max_output_tokens: int = Field(default=8192, ge=8192, le=32768)
    thinking_level: Literal["low", "medium", "high"] = "low"
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


def prefix(review_id):
    return "audio-review:" + str(review_id)
