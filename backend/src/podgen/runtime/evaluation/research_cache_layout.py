"""Research cache v2: an immutable catalog prefix followed by invocation-local work."""

import json

from podgen.domain.errors import ContractError

VERSION = "research-role-prefix/2"
BREAKPOINT = {"mode": "explicit"}
STABLE = (
    "output_language",
    "audience",
    "format",
    "genre",
    "assignment",
    "craft_reference",
    "accepted_plan",
    "research_contents",
    "source_catalog",
    "source_bibliography",
    "factual_remit",
    "research_cautions",
)


def content(payload, stage, *, stable_support_ids=None):
    from .reading_workflow import base_stage

    base = "plan-review" if stage.startswith("plan-review") else base_stage(stage)
    remaining = dict(payload)
    stable = {key: remaining.pop(key) for key in STABLE if key in remaining}
    if base == "draft":
        for key in ("story", "support"):
            if key in remaining:
                stable[key] = remaining.pop(key)
    if base == "revise" and "support" in remaining:
        # Only plan-selected sections stay in the cross-revision prefix. An extra
        # section commissioned by the current factual repair belongs to its tail.
        selected = set(stable_support_ids or ())
        support = remaining.pop("support")
        stable["support"] = [row for row in support if row.get("card_id", row.get("label")) in selected]
        extra = [row for row in support if row.get("card_id", row.get("label")) not in selected]
        if extra:
            remaining["additional_support"] = extra
    # editorial_frame may reflect the writer's changed composition; keep that
    # actual frame with the current task. accepted_plan, when offered, is stable.
    blocks = []
    for value, cached in ((stable, True), (remaining, base != "reader-review")):
        if value:
            part = dict(
                type="input_text",
                text=json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
            )
            if cached:
                part["prompt_cache_breakpoint"] = BREAKPOINT.copy()
            blocks.append(part)
    return blocks


def mark_turn(body):
    """Mark the last result in each of the two most recent tool-result groups."""
    if body.get("metadata", {}).get("podgen_prompt_cache_layout") != VERSION:
        return
    ends = []
    for index, item in enumerate(body["input"]):
        if item.get("type") == "function_call_output":
            item.pop("prompt_cache_breakpoint", None)
            if (
                index + 1 == len(body["input"])
                or body["input"][index + 1].get("type") != "function_call_output"
            ):
                ends.append(index)
    for index in ends[-2:]:
        body["input"][index]["prompt_cache_breakpoint"] = BREAKPOINT.copy()
    validate_boundaries(body)


def validate_boundaries(body):
    def count(value):
        if isinstance(value, dict):
            return sum(
                isinstance(child, dict)
                and (
                    key == "cache_control"
                    and child.get("type") == "ephemeral"
                    or key == "prompt_cache_breakpoint"
                    and child.get("mode") == "explicit"
                )
                for key, child in value.items()
            ) + sum(count(v) for v in value.values())
        if isinstance(value, list):
            return sum(count(v) for v in value)
        return 0

    if count(body) > 4:
        raise ContractError("prompt_cache_breakpoint_limit_exceeded")
