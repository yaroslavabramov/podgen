from typing import Literal

from pydantic import Field

from podgen.domain.contracts.common import UUID, ArtifactRef, Contract, PositiveInt, RuntimeRef

Stage = Literal["draft", "review", "revise"]
STAGES = ("draft", "review", "revise")


class RunLimits(Contract):
    max_total_input_tokens: PositiveInt = 900000
    max_total_output_tokens: PositiveInt = 196608
    max_elapsed_seconds: PositiveInt = 7200


class WritingRunCommand(Contract):
    run_id: UUID
    project_id: UUID
    actor_id: UUID
    case_id: Literal["gps-clock", "snow-pump"]
    author_ref: RuntimeRef
    writer_profile_ref: ArtifactRef
    reviewer_profile_ref: ArtifactRef
    repeat_index: int = Field(default=0, ge=0)
    review_anchor_policy: Literal["strict", "bounded-selection-v1"] = "strict"
    limits: RunLimits = Field(default_factory=RunLimits)


class RunFailure(Exception):
    def __init__(self, code, *, run_id=None, invocation_id=None):
        self.code, self.run_id, self.invocation_id = code, run_id, invocation_id
        super().__init__(code)


def command_payload(command):
    """Keep fingerprints of pre-policy strict commands stable."""
    payload = command.model_dump(mode="json")
    if command.review_anchor_policy == "strict":
        payload.pop("review_anchor_policy")
    return payload
