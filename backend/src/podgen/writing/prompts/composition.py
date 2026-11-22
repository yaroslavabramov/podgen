"""Explicit Markdown includes. Text owns wording; Python owns composition and identity."""

import hashlib
import json
from importlib.resources import files
from pathlib import PurePosixPath

from podgen.domain.errors import ContractError

ROLES = {
    "draft": "author",
    "review": "script-editor",
    "revise": "script-revision",
    "plan": "planner",
    "plan-review": "plan-editor",
    "plan-revise": "plan-revision",
    "reader-review": "listener-review",
    "fact-review": "fact-review",
    "dual-revise": "author-revision",
    "revision-verification": "revision-verification",
    "fact-verification": "fact-verification",
    "anchor-repair": "anchor-repair",
    "plan-target-repair": "plan-target-repair",
}
ORDINARY = {"draft", "review", "revise", "plan", "plan-review", "plan-revise"}
PASSTHROUGH = {"reader-review", "revision-verification", "anchor-repair", "plan-target-repair"}


def read(path, *, historical=False):
    name = PurePosixPath(path)
    if name.is_absolute() or ".." in name.parts or not name.parts:
        raise ContractError("invalid_writing_prompt_path")
    from .snapshot import current

    snapshot = current()
    key = (historical, path)
    if snapshot is not None and key in snapshot:
        return snapshot[key]
    text = (
        files("podgen.writing.prompts")
        .joinpath("history" if historical else "current", *name.parts)
        .read_bytes()
        .decode("utf-8")
    )
    if snapshot is not None:
        snapshot[key] = text
    return text


def render(path, *, format="solo", sources="cards", **options):
    from .markdown import render as compose

    if format not in ("solo", "dialogue") or sources not in ("cards", "research", "sources"):
        raise ContractError("unsupported_writing_prompt_context")
    return compose(path, dict(format=format, sources=sources, **options), read)


def identity(role, body):
    prefix = "whole-episode-a" if role in ORDINARY else role
    return prefix + "/sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def body(role, format="solo", *, sources="cards"):
    if role not in ROLES:
        raise ValueError("unknown_writing_prompt_role")
    if sources == "sources" and role not in ("fact-review", "fact-verification"):
        raise ContractError("unsupported_source_only_prompt_role")
    if sources == "research" and role in ("review", "revise"):
        raise ContractError("research_instruction_baseline_mismatch")
    return render(ROLES[role] + ".md", format=format, sources=sources)


def research(role, format):
    if role == "revise":
        raise ContractError("research_instruction_baseline_mismatch")
    return body(role, format, sources="research")


def historical_adapter(catalog, original, *, prefix=""):
    index = json.loads(read(catalog, historical=True))
    path = index.get(prefix + hashlib.sha256(original.encode("utf-8")).hexdigest())
    return read(path, historical=True) if path else None


def adapt_research(role, format, original):
    if format not in ("solo", "dialogue"):
        raise ContractError("unsupported_research_instruction_format")
    if role in PASSTHROUGH:
        return original
    if role not in ROLES or role == "review":
        raise ContractError("unsupported_research_instruction_role")
    result = historical_adapter("research-adapters.json", original, prefix=role + "/" + format + "/")
    if result is None:
        raise ContractError("research_instruction_baseline_mismatch")
    return result


def sources_only(role, format):
    return body(role, format, sources="sources")


def adapt_sources(original):
    result = historical_adapter("source-adapters.json", original)
    if result is None:
        raise ContractError("fact_source_only_instruction_baseline_mismatch")
    return result
