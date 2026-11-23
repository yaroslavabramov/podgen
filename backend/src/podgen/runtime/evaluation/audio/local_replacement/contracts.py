from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from podgen.audio.recognition.contracts import RecognitionProfile
from podgen.domain.contracts.common import Contract, DecimalString, RuntimeRef, Text

from ..fidelity.contracts import RecognitionPrice
from ..subject_contracts import AudioSubject


class ProposalConfig(Contract):
    contract: Literal["audio-local-replacement-proposal/1"] = "audio-local-replacement-proposal/1"
    proposal_id: UUID
    comparison_run_id: UUID
    parent_subject: AudioSubject
    approximate_seconds: float = Field(ge=0, allow_inf_nan=False)
    recognition_audit_id: UUID | None = None
    sentence_offset: int = Field(default=0, ge=-20, le=20)
    sentence_count: int = Field(default=1, ge=1, le=2)
    context_seconds: float = Field(default=4, ge=1, le=20)
    reason: Text
    parent_proposal_ref: RuntimeRef | None = None

    @model_validator(mode="after")
    def master_only(self):
        if self.parent_subject.kind not in ("assembly", "local_master") or not self.reason.strip():
            raise ValueError("local_replacement_requires_master_and_human_reason")
        return self


class Approval(Contract):
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Text
    reservation_per_request_usd: DecimalString
    max_cost_usd: DecimalString
    max_requests: int = Field(default=1, ge=1, le=3)
    request_timeout_seconds: float = Field(default=900, ge=1, le=1800)

    @model_validator(mode="after")
    def budget(self):
        if not self.reason.strip() or not 0 < Decimal(self.reservation_per_request_usd) <= Decimal(
            self.max_cost_usd
        ):
            raise ValueError("local_replacement_invalid_approval_budget")
        return self


def prefix(proposal_id):
    return "local-replacement:" + str(proposal_id)


class LocalizationProfile(Contract):
    recognition: RecognitionProfile = Field(default_factory=RecognitionProfile)
    credential_env: Text = "FISH_API_KEY"
    price: RecognitionPrice

    @model_validator(mode="after")
    def nonnegative_price(self):
        if Decimal(self.price.usd_per_audio_minute) < 0:
            raise ValueError("local_replacement_invalid_asr_price")
        return self


class ProposalConfigV2(ProposalConfig):
    contract: Literal["audio-local-replacement-proposal/2"] = "audio-local-replacement-proposal/2"
    localization: LocalizationProfile


class ProposalConfigV3(ProposalConfigV2):
    """The range comes from a confirmed recognition finding, never from a time point."""

    contract: Literal["audio-local-replacement-proposal/3"] = "audio-local-replacement-proposal/3"
    approximate_seconds: None = None
    candidate_id: UUID

    @model_validator(mode="after")
    def finding_bound(self):
        if self.recognition_audit_id is None or self.sentence_offset:
            raise ValueError("local_replacement_finding_requires_its_audit_and_no_offset")
        return self


def load_proposal_config(value):
    model = (
        ProposalConfigV3
        if value.get("contract") == "audio-local-replacement-proposal/3"
        else ProposalConfigV2
    )
    return model.model_validate(value)


class LocalizationBudget(Contract):
    reservation_per_request_usd: DecimalString
    max_cost_usd: DecimalString
    max_requests: int = Field(default=1, ge=1, le=3)
    request_timeout_seconds: float = Field(default=900, ge=1, le=1800)

    @model_validator(mode="after")
    def valid_budget(self):
        if not 0 < Decimal(self.reservation_per_request_usd) <= Decimal(self.max_cost_usd):
            raise ValueError("local_replacement_invalid_localization_budget")
        return self


class ApprovalV2(Approval):
    localization_budget: LocalizationBudget


def contextual(payload):
    return payload["contract"] == "audio-local-replacement-draft/2"
