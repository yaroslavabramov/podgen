"""Lossless user-data blocks: stable role context precedes each invocation's changing work."""

import json

from podgen.writing.prompt_cache import VERSION, validate

from .reading_workflow import base_stage

BREAKPOINT = {"mode": "explicit"}
COMMON = ("output_language", "audience", "format", "genre", "assignment", "craft_reference", "accepted_plan")
STATIC = {
    "plan": ("dossier", "material_catalog"),
    "plan-revise": ("dossier", "material_catalog"),
    "plan-review": ("dossier", "material_catalog", "review_target_root"),
    "fact-review": ("factual_remit", "support", "evidence", "necessary_limits"),
    "draft": ("story", "support"),
}


def _refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "material_refs":
                yield from child
            else:
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def role(stage):
    return "plan-review" if stage.startswith("plan-review") else base_stage(stage)


def content(payload, stage, *, version):
    validate(version)
    remaining = dict(payload)
    base = role(stage)
    reusable = base in ("plan-review", "reader-review", "fact-review", "revise")
    blocks = []

    def append(value, *, cached=False):
        if value:
            part = dict(
                type="input_text",
                text=json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
            )
            if cached:
                part["prompt_cache_breakpoint"] = BREAKPOINT.copy()
            blocks.append(part)

    context = {key: remaining.pop(key) for key in COMMON if key in remaining}
    append(context, cached=reusable)
    static = {key: remaining.pop(key) for key in STATIC.get(base, ()) if key in remaining}
    if base == "revise" and "support" in remaining:
        selected = set(_refs(context.get("accepted_plan", {})))
        support = remaining.pop("support")
        stable = [card for card in support if card.get("card_id", card.get("label")) in selected]
        extra = [card for card in support if card.get("card_id", card.get("label")) not in selected]
        static["support"] = stable
        if extra:
            remaining["additional_support"] = extra
    append(static, cached=reusable)
    append(remaining)
    return blocks


def apply(body, payload, stage, profile, version):
    if version is None:
        return body
    body["input"] = [dict(role="user", content=content(payload, stage, version=version))]
    body["metadata"]["podgen_prompt_cache_layout"] = VERSION
    base = role(stage)
    body["text"]["format"]["name"] = (
        "episode_script"
        if base in ("draft", "revise")
        else "editorial_feedback"
        if base == "review"
        else base.replace("-", "_")
    )
    if profile.provider == "openai":
        body["prompt_cache_options"] = {"mode": "explicit"}
    return body
