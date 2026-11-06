"""Planning wire outputs; the runtime pins inputs, resolves locators and assigns artifact IDs.

These proposals are not canonical StoryPlan/AcceptanceRecord artifacts. Structural
checks establish addressability and explicit decision consistency, not factual truth.
"""

import re
from collections.abc import Collection
from typing import Annotated, Literal

from pydantic import Field, model_serializer, model_validator

from podgen.domain.contracts.legacy_fields import omit_unsupplied

from .roles import FullText, RoleOutput


class PlanningSecondsRange(RoleOutput):
    min_seconds: Annotated[int, Field(ge=0)]
    max_seconds: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def ordered(self):
        if self.max_seconds < self.min_seconds:
            raise ValueError("planning_seconds_range_reversed")
        return self


class PlanningOpening(RoleOutput):
    approach: FullText
    material_refs: list[FullText] = Field(
        description="Exact locators offered in the supplied material catalog."
    )
    question_to_open: FullText


class PlanningTransitions(RoleOutput):
    from_previous: FullText
    toward_next: FullText


class PlanningAct(RoleOutput):
    act_id: FullText = Field(description="Short local label for this act, not a technical UUID.")
    title: FullText
    listener_question: FullText
    intended_change_in_understanding: FullText
    prerequisite_concepts: list[FullText]
    material_refs: list[FullText] = Field(
        description="Exact supplied locators supporting this act's reasoning."
    )
    limitations_to_voice: list[FullText] = Field(default_factory=list)
    transitions: PlanningTransitions
    promises_opened: list[FullText]
    promises_addressed: list[FullText]
    estimated_seconds_range: PlanningSecondsRange
    priority: Literal["essential", "optional"]

    @model_serializer(mode="wrap")
    def preserve_legacy_fields(self, handler):
        return omit_unsupplied(self, handler, "limitations_to_voice")


class PlanningEnding(RoleOutput):
    answer_to_deliver: FullText
    qualifications: list[FullText] = Field(default_factory=list)
    promises_to_close: list[FullText]

    @model_serializer(mode="wrap")
    def preserve_legacy_fields(self, handler):
        return omit_unsupplied(self, handler, "qualifications")


class PlanningHostFunction(RoleOutput):
    speaker_id: FullText = Field(description="Exact identifier from the assignment.")
    contribution: FullText
    interaction_guidance: FullText
    speech_register: FullText


class PlanningDurationStrategy(RoleOutput):
    estimated_seconds_range: PlanningSecondsRange
    assumptions: list[FullText]
    optional_material: list[FullText]


class PlanningResearchRequest(RoleOutput):
    question: FullText
    why_needed: FullText
    affected_act_ids: list[FullText] = Field(
        description="Local act labels; empty for an episode-wide question."
    )
    material_refs: list[FullText] = Field(
        description="Relevant supplied locators; may be empty for missing evidence."
    )
    blocking: bool = Field(description="Whether the promised answer requires this unresolved material.")


def _validate_requests(requests, act_ids):
    for request in requests:
        if set(request.affected_act_ids) - act_ids:
            raise ValueError("planning_unknown_research_act")


def _validate_materials(groups, available):
    allowed = set(available)
    if any(ref not in allowed for group in groups for ref in group):
        raise ValueError("planning_unknown_material_locator")


class PlanProposal(RoleOutput):
    main_question: FullText
    listener_promise: FullText
    working_answer: FullText = Field(
        description="Provisional supported answer, including justified uncertainty."
    )
    route_summary: FullText = Field(description="Connected route from initial understanding to the ending.")
    opening: PlanningOpening
    acts: list[PlanningAct] = Field(min_length=1)
    ending: PlanningEnding
    host_functions: list[PlanningHostFunction] = Field(min_length=1)
    duration_strategy: PlanningDurationStrategy

    @model_validator(mode="after")
    def local_labels(self):
        act_ids = {act.act_id for act in self.acts}
        if len(act_ids) != len(self.acts):
            raise ValueError("planning_duplicate_act_id")
        if len({host.speaker_id for host in self.host_functions}) != len(self.host_functions):
            raise ValueError("planning_duplicate_speaker_id")
        return self


class PlanningOutput(RoleOutput):
    status: Literal["ready", "needs_research"]
    plan: PlanProposal | None = Field(
        description="Complete proposal, or null when material cannot support one."
    )
    limitations: list[FullText] = Field(default_factory=list)
    research_requests: list[PlanningResearchRequest]

    @model_serializer(mode="wrap")
    def preserve_legacy_fields(self, handler):
        return omit_unsupplied(self, handler, "limitations")

    @model_validator(mode="after")
    def proposal_state(self):
        blocked = any(request.blocking for request in self.research_requests)
        if blocked != (self.status == "needs_research"):
            raise ValueError("planning_research_status_mismatch")
        if self.status == "ready" and self.plan is None:
            raise ValueError("planning_ready_without_plan")
        _validate_requests(
            self.research_requests, {act.act_id for act in self.plan.acts} if self.plan else set()
        )
        return self

    def validate_against(self, *, material_locators: Collection[str], speaker_ids: Collection[str]) -> None:
        """Bind offered locators and assigned speakers without judging the proposed story."""
        if self.plan and {host.speaker_id for host in self.plan.host_functions} != set(speaker_ids):
            raise ValueError("planning_speaker_assignment_mismatch")
        groups = [request.material_refs for request in self.research_requests]
        if self.plan:
            groups += [self.plan.opening.material_refs] + [act.material_refs for act in self.plan.acts]
        _validate_materials(
            groups,
            material_locators,
        )


class PlanReviewStrength(RoleOutput):
    targets: list[FullText] = Field(min_length=1, description="JSON Pointers into the exact supplied plan.")
    reason: FullText


class PlanReviewFinding(RoleOutput):
    targets: list[FullText] = Field(min_length=1, description="JSON Pointers into the exact supplied plan.")
    priority: Literal["blocking", "important", "optional"]
    observation: FullText
    requested_change: FullText
    preserve: list[FullText]
    material_refs: list[FullText]


def _validate_target(document, pointer):
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("planning_review_invalid_target")
    value = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token) and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError("planning_review_unknown_target")


class PlanReviewOutput(RoleOutput):
    decision: Literal["accept", "revise", "needs_research"]
    decision_reason: FullText
    listener_answer: FullText
    important_limits: list[FullText] = Field(default_factory=list)
    strengths: list[PlanReviewStrength]
    findings: list[PlanReviewFinding]
    research_requests: list[PlanningResearchRequest]
    duration_note: FullText

    @model_serializer(mode="wrap")
    def preserve_legacy_fields(self, handler):
        return omit_unsupplied(self, handler, "important_limits")

    @model_validator(mode="after")
    def decision_consistency(self):
        blocked = any(request.blocking for request in self.research_requests)
        if blocked != (self.decision == "needs_research"):
            raise ValueError("planning_research_decision_mismatch")
        if self.decision == "accept" and any(f.priority != "optional" for f in self.findings):
            raise ValueError("planning_accept_with_required_changes")
        return self

    def validate_against(self, plan: PlanningOutput, *, material_locators: Collection[str]) -> None:
        """Check addresses against this plan; the caller must bind its exact saved version."""
        self.validate_context(plan, material_locators=material_locators)
        document = plan.model_dump(mode="json")
        for entry in (*self.strengths, *self.findings):
            for target in entry.targets:
                _validate_target(document, target)

    def validate_context(self, plan: PlanningOutput, *, material_locators: Collection[str]) -> None:
        """Validate all non-address bindings; strict validate_against always also checks every target."""
        if self.decision == "accept" and plan.status != "ready":
            raise ValueError("planning_accept_with_unresolved_research")
        _validate_requests(
            self.research_requests, {act.act_id for act in plan.plan.acts} if plan.plan else set()
        )
        _validate_materials(
            [finding.material_refs for finding in self.findings]
            + [request.material_refs for request in self.research_requests],
            material_locators,
        )
