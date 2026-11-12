import json
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


class FidelityConfigBase(Contract):
    audit_id: UUID
    comparison_run_id: UUID
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


class FidelityConfig(FidelityConfigBase):
    contract: Literal["audio-fidelity-evaluation/1"] = "audio-fidelity-evaluation/1"
    result_ref: RuntimeRef

    @property
    def subject_ref(self):
        return self.result_ref

    @property
    def subject_kind(self):
        return "comparison_result"


class AudioSubject(Contract):
    kind: Literal["comparison_result", "assembly"]
    ref: RuntimeRef


class FidelityConfigV2(FidelityConfigBase):
    contract: Literal["audio-fidelity-evaluation/2"] = "audio-fidelity-evaluation/2"
    subject: AudioSubject

    @property
    def subject_ref(self):
        return self.subject.ref

    @property
    def subject_kind(self):
        return self.subject.kind


def load_fidelity_config(text):
    raw = json.loads(text)
    return (
        FidelityConfigV2 if raw.get("contract") == "audio-fidelity-evaluation/2" else FidelityConfig
    ).model_validate(raw)


def prefix(audit_id):
    return "fidelity:" + str(audit_id)
