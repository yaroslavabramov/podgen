from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from podgen.domain.contracts.common import Contract, DecimalString, RuntimeRef, Text

from ..subject_contracts import AudioSubject, SynthesisPart


class RepairConfig(Contract):
    contract: Literal["audio-confirmed-fragment-repair/1"] = "audio-confirmed-fragment-repair/1"
    repair_id: UUID
    comparison_run_id: UUID
    parent_subject: AudioSubject
    part_source: SynthesisPart
    review_id: UUID
    review_result_ref: RuntimeRef
    candidate_id: Text
    reason: Text
    reservation_per_request_usd: DecimalString
    max_cost_usd: DecimalString
    max_requests: int = Field(default=1, ge=1, le=3)
    request_timeout_seconds: float = Field(default=900, ge=1, le=1800)
    max_quality_depth: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def limits(self):
        if not self.reason.strip() or not 0 < Decimal(self.reservation_per_request_usd) <= Decimal(
            self.max_cost_usd
        ):
            raise ValueError("invalid_repair_reason_or_budget")
        return self


def prefix(repair_id):
    return "fragment-repair:" + str(repair_id)
