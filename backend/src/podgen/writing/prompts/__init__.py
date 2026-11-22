"""Editable current Markdown composition; immutable history is a separate namespace."""

from dataclasses import dataclass
from typing import Literal

from . import composition

Stage = Literal["draft", "review", "revise", "plan", "plan-review", "plan-revise"]
Format = Literal["solo", "dialogue"]
_HISTORICAL = {"whole-episode-a/1": "v1", "whole-episode-a/2": "v2", "whole-episode-a/3": "v3"}


@dataclass(frozen=True)
class RoleInstructions:
    version: str
    body: str


def load_role(role, format="solo", *, sources="cards"):
    text = composition.body(role, format, sources=sources)
    return RoleInstructions(composition.identity(role, text), text)


def load_instruction_set(format: Format, *, include_planning=False):
    stages = (
        ("draft", "review", "revise", "plan", "plan-review", "plan-revise")
        if include_planning
        else ("draft", "review", "revise")
    )
    return {stage: load_role(stage, format) for stage in stages}


def load_anchor_repair_instructions():
    return load_role("anchor-repair")


def load_plan_target_repair_instructions():
    return load_role("plan-target-repair")


def role_instructions(stage: Stage, format: Format):
    return load_role(stage, format).body


def role_instructions_for_version(stage: Stage, format: Format, *, version: str):
    if stage not in ("draft", "review", "revise") or format not in ("solo", "dialogue"):
        raise ValueError("unknown_historical_writing_role")
    if version not in _HISTORICAL:
        raise ValueError(f"unknown_writing_instruction_version: {version!r}")
    prefix = "numbered/" + _HISTORICAL[version] + "/"
    parts = [
        composition.read(prefix + name + ".md", historical=True).replace("\r\n", "\n").replace("\r", "\n")
        for name in ("common", format, stage)
    ]
    return f"Instructions: {version}; stage={stage}; format={format}\n\n" + "\n\n".join(parts)


def load_reading_instructions(stage):
    if stage not in (
        "reader-review",
        "fact-review",
        "dual-revise",
        "revision-verification",
        "fact-verification",
    ):
        raise ValueError("unknown_reading_prompt")
    return load_role(stage)
