"""One planned encounter per scene. Editorial reasoning stays in addressed notes."""

from typing import Literal

from pydantic import Field, model_validator

from .planning_choice import PlanningChoiceOutput
from .planning_roles import (
    PlanningDurationStrategy,
    PlanningHostFunction,
    PlanningSecondsRange,
    _validate_materials,
)
from .roles import FullText, RoleOutput


class SceneDevelopment(RoleOutput):
    development: FullText = Field(
        description="What listeners encounter in order: the action, observation, demonstration or thought that changes the situation and carries the story forward. Give enough specific material to write this scene."
    )
    material_refs: list[FullText] = Field(description="Offered material supporting this scene.")


class ActDevelopment(SceneDevelopment):
    act_id: FullText = Field(description="Unique short act label. The names opening and ending are reserved.")
    title: FullText
    estimated_seconds_range: PlanningSecondsRange
    priority: Literal["essential", "optional"]


class ScenePlan(RoleOutput):
    main_question: FullText
    listener_promise: FullText
    working_answer: FullText = Field(
        description="The episode-wide answer, stated as precisely as the reporting allows."
    )
    opening: SceneDevelopment
    acts: list[ActDevelopment] = Field(min_length=1)
    ending: SceneDevelopment
    host_functions: list[PlanningHostFunction] = Field(min_length=1)
    duration_strategy: PlanningDurationStrategy

    @model_validator(mode="after")
    def local_labels(self):
        labels = [act.act_id for act in self.acts]
        if {"opening", "ending"}.intersection(labels) or len(labels) != len(set(labels)):
            raise ValueError("planning_ambiguous_scene_id")
        if len({host.speaker_id for host in self.host_functions}) != len(self.host_functions):
            raise ValueError("planning_duplicate_speaker_id")
        return self


class EditorialNote(RoleOutput):
    scene: FullText = Field(description="The exact act_id, opening or ending.")
    kind: Literal["editorial_choice", "claim_scope"]
    note: FullText = Field(
        description="An editorial choice and its reason, or the precise distinction a primary source supports for a particular claim. An internal note for review."
    )
    material_refs: list[FullText] = Field(
        description="Sources bearing on this note. A claim_scope needs at least one."
    )


class ScenePlanningOutput(PlanningChoiceOutput):
    plan: ScenePlan | None
    editorial_notes: list[EditorialNote] = Field(
        description="Record consequential source or selection judgments separately from the listening order. Empty when none need recording."
    )

    @model_validator(mode="after")
    def bind_notes(self):
        scenes = {"opening", "ending", *(act.act_id for act in self.plan.acts)} if self.plan else set()
        for note in self.editorial_notes:
            if note.scene not in scenes:
                raise ValueError("planning_note_unknown_scene")
            if note.kind == "claim_scope" and not note.material_refs:
                raise ValueError("planning_claim_scope_requires_source")
        return self

    def validate_against(self, *, material_locators, speaker_ids):
        super().validate_against(material_locators=material_locators, speaker_ids=speaker_ids)
        groups = [note.material_refs for note in self.editorial_notes]
        if self.plan:
            groups.append(self.plan.ending.material_refs)
        _validate_materials(groups, material_locators)


def is_scene_plan(output):
    return output is not None and "editorial_notes" in output


def claim_scopes(output):
    if not is_scene_plan(output):
        return []
    return [
        dict(claim_scope=note["note"], material_refs=list(note["material_refs"]))
        for note in output["editorial_notes"]
        if note["kind"] == "claim_scope"
    ]
