"""Independent planning output pin: brief alternative concepts, one fully developed story."""

from typing import Literal

from pydantic import Field, model_validator

from podgen.domain.errors import ContractError

from .planning_v9 import PlanningOutputV9
from .projection_versions import V9, manifest_version
from .roles import FullText, RoleOutput

KEY = "planning_output_contract"
VERSION = "planning-story-choice/1"
SCENE_VERSION = "planning-scene-development/1"


class StorySelection(RoleOutput):
    first: FullText = Field(description="Brief first concept: the story's movement and where it leads.")
    second: FullText = Field(
        description="Brief genuinely different telling, not a paraphrase or stylistic variant."
    )
    difference: FullText = Field(description="The substantive editorial difference between these tellings.")
    selected: Literal["first", "second"]
    why_selected: FullText = Field(
        description="Brief editorial result: what the selected telling offers this audience within the brief. Not a reasoning transcript."
    )


class PlanningChoiceOutput(PlanningOutputV9):
    story_selection: StorySelection | None = Field(
        description="Compare two distinct tellings before developing the selected one in plan. Null is allowed only when no plan is supported."
    )

    @model_validator(mode="after")
    def selection_for_plan(self):
        if self.plan is not None and self.story_selection is None:
            raise ValueError("planning_story_selection_required")
        return self


def validate(version, projection):
    if version is not None and (version not in (VERSION, SCENE_VERSION) or projection != V9):
        raise ContractError("unsupported_planning_output_contract")
    return version


def for_input(input_contract, projection):
    return VERSION if input_contract == "editorial-role-packets/1" and projection == V9 else None


def from_manifest(manifest):
    return validate(manifest.get(KEY), manifest_version(manifest))


def describe_schema(schema):
    # Put the brief choice before the developed plan in the requested output order.
    properties = schema["properties"]
    schema["properties"] = {"story_selection": properties["story_selection"], **properties}
    schema["required"] = [key for key in schema["properties"] if key in schema["required"]]
