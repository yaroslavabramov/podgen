from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from podgen.audio.recognition.contracts import RecognitionProfile
from podgen.domain.contracts.common import Contract, DecimalString, RuntimeRef, Text


class RecognitionPrice(Contract):
    usd_per_audio_minute: DecimalString
    source_url: Text
    checked_at: Text


class FidelityConfig(Contract):
    contract: Literal["audio-fidelity-evaluation/1"] = "audio-fidelity-evaluation/1"
    audit_id: UUID
    comparison_run_id: UUID
    result_ref: RuntimeRef
    recognition: RecognitionProfile = Field(default_factory=RecognitionProfile)
    credential_env: Text = "FISH_API_KEY"
    price: RecognitionPrice
    max_requests: Literal[1] = 1
    max_cost_usd: DecimalString = "0.25"
    reservation_usd: DecimalString
    clip_margin_seconds: float = Field(default=1.5, ge=0, le=10)
    max_review_clip_seconds: float = Field(default=45, ge=1, le=45)

    @model_validator(mode="after")
    def valid_budget(self):
        if (
            not 0 < Decimal(self.reservation_usd) <= Decimal(self.max_cost_usd)
            or Decimal(self.price.usd_per_audio_minute) < 0
        ):
            raise ValueError("invalid_recognition_budget")
        return self


def prefix(audit_id):
    return "fidelity:" + str(audit_id)
