"""V9 separates an episode's supported answer from the chosen final listening moment."""

from pydantic import Field

from .planning_roles import PlanningOutput, PlanProposal
from .roles import FullText, RoleOutput


class PlanningFinalMoment(RoleOutput):
    last_moment: FullText = Field(
        description="The chosen last action, observation, discovery or thought the listener encounters; not a recap of the episode's answer or prescribed spoken lines."
    )
    why_end_here: FullText = Field(
        description="Why this is the stopping point for the chosen story and promise; what remains with the listener without another summary."
    )
    promises_to_close: list[FullText]


class PlanProposalV9(PlanProposal):
    ending: PlanningFinalMoment


class PlanningOutputV9(PlanningOutput):
    plan: PlanProposalV9 | None = Field(
        description="Complete selected story, or null when material cannot support one."
    )


def describe_plan(schema):
    from .planning_descriptions import STORY_DESCRIPTIONS

    definitions = schema["$defs"]
    for name, fields in STORY_DESCRIPTIONS.items():
        if name == "PlanningEnding":
            continue
        node = definitions["PlanProposalV9" if name == "PlanProposal" else name]
        for field, description in fields.items():
            node["properties"][field]["description"] = description
    definitions["PlanningAct"]["properties"]["prerequisite_concepts"]["description"] = (
        "Situational premises or concepts this audience needs before following this part; establish them earlier or introduce them before the dependent question or discovery. Empty is valid when none are needed."
    )
    proposal = definitions["PlanProposalV9"]["properties"]
    proposal["working_answer"]["description"] = (
        "The supported meaning or answer established across the complete episode, including uncertainty that changes it. This is not a required final speech or the selected last moment."
    )
    proposal["main_question"]["description"] = (
        "The central question of the selected story for this audience; preserve an explicit client question, and prepare the premises a newcomer needs to follow it."
    )
    proposal["route_summary"]["description"] = (
        "The central story from its prepared opening through connected developments to the chosen last moment. Select or combine branches that repeat the same contribution; keep event chronology and the timing of discoveries accurate."
    )


def describe_review(schema):
    properties = schema["properties"]
    properties["listener_answer"]["description"] = (
        "Follow this selected central story as a newcomer: the premises available before the opening question, developments and first disclosures, chronology of actions and knowledge, the supported episode-wide answer and the chosen last moment. Identify duplicate branches or another conclusion after that stopping point."
    )
    properties["findings"]["description"] = (
        "Addressed problems in the selected story, novice prerequisites, opening promise and its resolution, chronological implications, overlapping branches or chosen ending. An episode-wide answer is not a required closing recap. Empty is valid when the plan works."
    )
