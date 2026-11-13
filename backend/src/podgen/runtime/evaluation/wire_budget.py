"""Measure exactly the three model-visible wire components, before constructing any provider."""

import json

from podgen.domain.errors import ContractError
from podgen.writing.projections import CONTRACT, SINGLE_INPUT_TOKEN_LIMIT, VISIBLE_BYTES_LIMIT

LEGACY_INPUT_BUDGET = dict(visible_bytes=VISIBLE_BYTES_LIMIT, input_tokens=SINGLE_INPUT_TOKEN_LIMIT)
PRODUCTION_INPUT_BUDGET = dict(visible_bytes=256 * 1024, input_tokens=65536)
MODEL_WINDOW_BUDGET = dict(version="model-context/1", visible_bytes=None, input_tokens=None)
# Headroom for the already allowed reading/tool turns. Cached and repeated input
# still counts against the technical run ceiling; this does not increase stage/turn limits.
PRODUCTION_RUN_LIMITS = dict(
    max_total_input_tokens=4194304, max_total_output_tokens=393216, max_elapsed_seconds=14400
)


def input_budget_for_profile(name, *, model_window=False):
    if model_window:
        return MODEL_WINDOW_BUDGET.copy()
    return (PRODUCTION_INPUT_BUDGET if name == "production/1" else LEGACY_INPUT_BUDGET).copy()


def validate_manifest_budget(manifest):
    budget = manifest.get("role_input_budget")
    if budget == MODEL_WINDOW_BUDGET:
        if manifest.get("source_tool_contract", {}).get("version") != "frozen-corpus-tools/7":
            raise ContractError("model_window_requires_durable_research_runtime")
        return budget.copy()
    if budget != input_budget_for_profile(manifest.get("behavior_profile")):
        raise ContractError("role_input_contract_changed")
    return budget.copy()


def visible_sizes(body):
    values = dict(
        instructions=body["instructions"], input=body["input"], schema=body["text"]["format"]["schema"]
    )
    if body.get("tools"):
        values["tools"] = body["tools"]
    sizes = {
        name: len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
        for name, value in values.items()
    }
    return dict(**sizes, total=sum(sizes.values()))


def check_visible(body, contract, *, max_visible_bytes=VISIBLE_BYTES_LIMIT):
    if contract is None:
        return
    if contract != CONTRACT:
        raise ContractError("unknown_role_input_contract")
    if max_visible_bytes is not None and visible_sizes(body)["total"] > max_visible_bytes:
        raise ContractError("role_visible_byte_budget_exceeded")


def check_tokens(count, contract, *, max_input_tokens=SINGLE_INPUT_TOKEN_LIMIT):
    if contract == CONTRACT and count > max_input_tokens:
        raise ContractError("role_input_token_budget_exceeded")


def resolved_limits(limits, profile):
    if limits == MODEL_WINDOW_BUDGET:
        return dict(visible_bytes=None, input_tokens=profile.context_limit - profile.output_limit)
    return limits
