from typing import Literal

from pydantic import Field, field_validator, model_validator

from podgen.domain.contracts.common import UUID, ArtifactRef, Contract, PositiveInt, RuntimeRef

Stage = Literal["draft", "review", "revise", "plan", "plan-review", "plan-revise", "plan-review-revised"]
STAGES = ("draft", "review", "revise")
PLANNING_STAGES = ("plan", "plan-review", "plan-revise", "plan-review-revised")
ALL_STAGES = (*PLANNING_STAGES, *STAGES)


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
    plan_mode: Literal["fixed-plan", "own-plan"] = "fixed-plan"
    planner_profile_ref: ArtifactRef | None = None
    plan_reviewer_profile_ref: ArtifactRef | None = None
    max_plan_revisions: Literal[0, 1] = 1
    repeat_index: int = Field(default=0, ge=0)
    review_anchor_policy: Literal["strict", "bounded-selection-v1"] = "strict"
    limits: RunLimits = Field(default_factory=RunLimits)

    @field_validator("author_ref", mode="before")
    @classmethod
    def restore_staging_selector(cls, value):
        if isinstance(value, dict) and value.get("selector") == "staged-author":
            if set(value) != {"selector", "record_id"}:
                raise ValueError("invalid_staged_author_selector")
            return RuntimeRef(record_kind="user_input", record_id=value["record_id"])
        return value

    @model_validator(mode="after")
    def planning_profiles(self):
        if self.plan_mode == "own-plan" and (
            self.planner_profile_ref is None or self.plan_reviewer_profile_ref is None
        ):
            raise ValueError("own_plan_requires_explicit_profiles")
        if self.plan_mode == "fixed-plan" and (
            self.planner_profile_ref is not None or self.plan_reviewer_profile_ref is not None
        ):
            raise ValueError("planning_profiles_require_own_plan")
        return self


class RunFailure(Exception):
    def __init__(self, code, *, run_id=None, invocation_id=None):
        self.code, self.run_id, self.invocation_id = code, run_id, invocation_id
        super().__init__(code)


def command_payload(command):
    """Keep fingerprints of pre-policy strict commands stable."""
    payload = command.model_dump(mode="json")
    if command.review_anchor_policy == "strict":
        payload.pop("review_anchor_policy")
    if command.plan_mode == "own-plan":
        # The broad root is a selector, not a source dependency: its plan must not enter the read closure.
        payload["author_ref"] = {"selector": "staged-author", "record_id": str(command.author_ref.record_id)}
    if command.plan_mode == "fixed-plan":
        for field in ("plan_mode", "planner_profile_ref", "plan_reviewer_profile_ref", "max_plan_revisions"):
            payload.pop(field)
    return payload
