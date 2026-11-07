from typing import Literal

from pydantic import Field, field_validator, model_validator

from podgen.domain.contracts.common import UUID, ArtifactRef, Contract, PositiveInt, RuntimeRef

Stage = Literal[
    "draft",
    "review",
    "revise",
    "plan",
    "plan-review",
    "plan-revise",
    "plan-review-revised",
    "reader-review",
    "fact-review",
    "reader-review-1",
    "fact-review-1",
    "reader-review-2",
    "fact-review-2",
    "revise-1",
    "revise-2",
]
STAGES = ("draft", "review", "revise")
PLANNING_STAGES = ("plan", "plan-review", "plan-revise", "plan-review-revised")
ALL_STAGES = (*PLANNING_STAGES, *STAGES)


class RunLimits(Contract):
    max_total_input_tokens: PositiveInt = 196608
    max_total_output_tokens: PositiveInt = 131072
    max_elapsed_seconds: PositiveInt = 7200


class WritingRunCommand(Contract):
    run_id: UUID
    project_id: UUID
    actor_id: UUID
    case_id: str = Field(min_length=1)
    input_contract: Literal["editorial-role-packets/1", "legacy-author-materials/1"] = (
        "editorial-role-packets/1"
    )
    fixed_plan_input_ref: RuntimeRef | None = None
    author_ref: RuntimeRef
    writer_profile_ref: ArtifactRef
    reviewer_profile_ref: ArtifactRef
    plan_mode: Literal["fixed-plan", "own-plan"] = "fixed-plan"
    planner_profile_ref: ArtifactRef | None = None
    plan_reviewer_profile_ref: ArtifactRef | None = None
    max_plan_revisions: Literal[0, 1] = 1
    review_mode: Literal["informed-v1", "independent-readings/1"] = "independent-readings/1"
    max_script_revisions: Literal[0, 1, 2] = 2
    initial_script_ref: RuntimeRef | None = None
    planning_parent_run_id: UUID | None = None
    planning_continuation_limit: PositiveInt | None = None
    repeat_index: int = Field(default=0, ge=0)
    review_anchor_policy: Literal["strict", "bounded-selection-v1"] = "strict"
    limits: RunLimits = Field(default_factory=RunLimits)

    @model_validator(mode="before")
    @classmethod
    def legacy_review_mode(cls, value):
        if (
            isinstance(value, dict)
            and value.get("input_contract") == "legacy-author-materials/1"
            and "review_mode" not in value
        ):
            value = {**value, "review_mode": "informed-v1"}
        return value

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
        if (self.planning_parent_run_id is None) != (self.planning_continuation_limit is None):
            raise ValueError("planning_continuation_requires_parent_and_limit")
        if self.planning_parent_run_id is not None and (
            self.planning_parent_run_id == self.run_id
            or self.input_contract != "editorial-role-packets/1"
            or self.plan_mode != "own-plan"
            or self.max_plan_revisions != 1
        ):
            raise ValueError("invalid_planning_continuation")
        if self.plan_mode == "own-plan" and self.fixed_plan_input_ref is not None:
            raise ValueError("own_plan_forbids_fixed_input")
        if self.plan_mode == "own-plan" and (
            self.planner_profile_ref is None or self.plan_reviewer_profile_ref is None
        ):
            raise ValueError("own_plan_requires_explicit_profiles")
        if self.plan_mode == "fixed-plan" and (
            self.planner_profile_ref is not None or self.plan_reviewer_profile_ref is not None
        ):
            raise ValueError("planning_profiles_require_own_plan")
        if self.review_mode == "informed-v1" and self.initial_script_ref is not None:
            raise ValueError("initial_script_requires_independent_readings")
        if self.review_mode != "informed-v1" and self.input_contract != "editorial-role-packets/1":
            raise ValueError("independent_readings_require_editorial_inputs")
        if self.initial_script_ref is not None and self.plan_mode == "own-plan":
            raise ValueError("initial_script_does_not_require_new_plan")
        return self


class RunFailure(Exception):
    def __init__(self, code, *, run_id=None, invocation_id=None):
        self.code, self.run_id, self.invocation_id = code, run_id, invocation_id
        super().__init__(code)


def command_payload(command):
    """Keep fingerprints of pre-policy strict commands stable."""
    payload = command.model_dump(mode="json")
    if command.planning_parent_run_id is None:
        payload.pop("planning_parent_run_id")
        payload.pop("planning_continuation_limit")
    if command.review_mode == "informed-v1":
        for field in ("review_mode", "max_script_revisions", "initial_script_ref"):
            payload.pop(field)
    if command.review_anchor_policy == "strict":
        payload.pop("review_anchor_policy")
    if command.input_contract == "legacy-author-materials/1":
        payload.pop("input_contract")
        payload.pop("fixed_plan_input_ref")
    if command.plan_mode == "own-plan" and command.input_contract == "legacy-author-materials/1":
        # The broad root is a selector, not a source dependency: its plan must not enter the read closure.
        payload["author_ref"] = {"selector": "staged-author", "record_id": str(command.author_ref.record_id)}
    if command.plan_mode == "fixed-plan":
        for field in ("plan_mode", "planner_profile_ref", "plan_reviewer_profile_ref", "max_plan_revisions"):
            payload.pop(field)
    return payload
