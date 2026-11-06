"""Editable current components and byte-stable numbered historical instructions.

Current content IDs identify assemblies; they are not a global revision archive.
Resolve an older content ID through its run's archived PromptDefinition, not here.
"""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

Stage = Literal["draft", "review", "revise", "plan", "plan-review", "plan-revise"]
Format = Literal["solo", "dialogue"]
_STAGES = ("draft", "review", "revise")
_PLANNING_STAGES = ("plan", "plan-review", "plan-revise")
_FORMATS = ("solo", "dialogue")
_HISTORICAL = {"whole-episode-a/1": "v1", "whole-episode-a/2": "v2", "whole-episode-a/3": "v3"}


@dataclass(frozen=True)
class RoleInstructions:
    version: str
    body: str


def _validate(stage, format, *, historical=False):
    if stage not in (_STAGES if historical else (*_STAGES, *_PLANNING_STAGES)):
        raise ValueError(f"unknown_writing_stage: {stage!r}")
    if format not in _FORMATS:
        raise ValueError(f"unknown_writing_format: {format!r}")


def _read(root, names):
    # read_bytes avoids newline normalization: CRLF, whitespace and Unicode are meaningful.
    return {name: root.joinpath(name).read_bytes().decode("utf-8") for name in names}


def _assemble(stage, format, components):
    names = ("common.md", f"{format}.md", f"{stage}.md")
    # Ordered, framed names/content; no generated version/header participates in its own hash.
    source = json.dumps(
        ["semantic-body/1", stage, format, [[name, components[name]] for name in names]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    version = "whole-episode-a/sha256:" + hashlib.sha256(source).hexdigest()
    body = "\n\n".join(components[name] for name in names)
    return RoleInstructions(version, body)


def load_instruction_set(format: Format, *, include_planning=False) -> dict[str, RoleInstructions]:
    """Read each component once for a new run, then assemble all three stage bodies."""
    _validate("draft", format)
    stages = (*_STAGES, *_PLANNING_STAGES) if include_planning else _STAGES
    names = ("common.md", f"{format}.md", *(f"{stage}.md" for stage in stages))
    components = _read(files(__name__).joinpath("current"), names)
    return {stage: _assemble(stage, format, components) for stage in stages}


def load_anchor_repair_instructions() -> RoleInstructions:
    """Independent suffix: pin it at run creation; never re-read it during repair/resume."""
    raw = files(__name__).joinpath("current").joinpath("anchor-repair.md").read_bytes()
    return RoleInstructions("anchor-repair/sha256:" + hashlib.sha256(raw).hexdigest(), raw.decode("utf-8"))


def load_plan_target_repair_instructions() -> RoleInstructions:
    """Pin a dedicated address-selector suffix; it does not alter ordinary review IDs."""
    raw = files(__name__).joinpath("current").joinpath("plan-target-repair.md").read_bytes()
    return RoleInstructions(
        "plan-target-repair/sha256:" + hashlib.sha256(raw).hexdigest(), raw.decode("utf-8")
    )


def role_instructions(stage: Stage, format: Format) -> str:
    """Current complete body. Editing an applicable .md changes its content ID automatically."""
    _validate(stage, format)
    names = ("common.md", f"{format}.md", f"{stage}.md")
    return _assemble(stage, format, _read(files(__name__).joinpath("current"), names)).body


def role_instructions_for_version(stage: Stage, format: Format, *, version: str) -> str:
    """Read only published numbered history /1, /2, /3, with no current-content fallback."""
    _validate(stage, format, historical=True)
    if version not in _HISTORICAL:
        raise ValueError(f"unknown_writing_instruction_version: {version!r}")
    root = files(__name__).joinpath(_HISTORICAL[version])
    names = ("common.md", f"{format}.md", f"{stage}.md")
    # Preserve the published resolver's universal-newline behavior for numbered history.
    parts = {name: root.joinpath(name).read_text(encoding="utf-8") for name in names}
    return f"Instructions: {version}; stage={stage}; format={format}\n\n" + "\n\n".join(
        parts[name] for name in names
    )


def load_reading_instructions(stage) -> RoleInstructions:
    """Dedicated files only: no shared author, plan, format or factual hints in cold reading."""
    if stage not in ("reader-review", "fact-review", "dual-revise"):
        raise ValueError("unknown_reading_prompt")
    raw = files(__name__).joinpath("current").joinpath(stage + ".md").read_bytes()
    return RoleInstructions(stage + "/sha256:" + hashlib.sha256(raw).hexdigest(), raw.decode("utf-8"))
