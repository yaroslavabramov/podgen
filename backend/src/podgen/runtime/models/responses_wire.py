"""Cache metadata is transport structure, never part of serialized tool evidence."""

from copy import deepcopy

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.research_cache_layout import BREAKPOINT, VERSION, validate_boundaries


def responses_request(body):
    if body.get("metadata", {}).get("podgen_prompt_cache_layout") != VERSION:
        return body
    result = deepcopy(body)
    for item in result["input"]:
        if item.get("type") != "function_call_output":
            continue
        marker = item.pop("prompt_cache_breakpoint", None)
        if (marker is not None and marker != BREAKPOINT) or not isinstance(item["output"], str):
            raise ContractError("invalid_tool_result_cache_boundary")
        # All v2 results have one wire shape from their first appearance; sliding
        # a boundary never switches a historical result back to string output.
        item["output"] = [dict(type="input_text", text=item["output"])]
        if marker is not None:
            item["output"][0]["prompt_cache_breakpoint"] = marker
    validate_boundaries(result)
    return result
