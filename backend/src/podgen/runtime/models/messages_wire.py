"""Translate Podgen's text-generation request to Messages and back to the journal envelope."""

import json
from copy import deepcopy

from .contracts import ProviderFault


def _native_schema(node, *, output):
    if isinstance(node, list):
        return [_native_schema(item, output=output) for item in node]
    if not isinstance(node, dict):
        return node
    result = {key: _native_schema(value, output=output) for key, value in node.items()}
    if output and "minLength" in result:
        result["description"] = result.get("description", "") + f" Minimum length: {result.pop('minLength')}."
    constraints = {}
    for key in (
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "multipleOf",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "maxItems",
        "pattern",
    ):
        if key in result:
            constraints[key] = result.pop(key)
    if result.get("minItems", 0) > 1:
        constraints["minItems"] = result.pop("minItems")
    if constraints:
        description = json.dumps(constraints, separators=(",", ":"))
        result["description"] = (
            result.get("description", "") + " Required constraints: " + description + "."
        ).strip()
    if "oneOf" in result:
        if "anyOf" in result:
            raise ProviderFault("unsupported_anthropic_union")
        result["anyOf"] = result.pop("oneOf")
    result.pop("discriminator", None)
    return result


def _text_block(piece):
    if piece["type"] != "input_text":
        raise ProviderFault("unsupported_anthropic_input")
    block = {"type": "text", "text": piece["text"]}
    if "prompt_cache_breakpoint" in piece:
        if piece["prompt_cache_breakpoint"] != {"mode": "explicit"}:
            raise ProviderFault("unsupported_anthropic_cache_breakpoint")
        block["cache_control"] = {"type": "ephemeral", "ttl": "5m"}
    return block


def messages_request(body, *, counting=False):
    messages = []
    for message in body["input"]:
        if message.get("type") == "anthropic_assistant":
            messages.append(dict(role="assistant", content=deepcopy(message["content"])))
            continue
        if message.get("type") == "function_call_output":
            block = dict(type="tool_result", tool_use_id=message["call_id"], content=message["output"])
            if "prompt_cache_breakpoint" in message:
                if body.get("metadata", {}).get(
                    "podgen_prompt_cache_layout"
                ) != "research-role-prefix/2" or message["prompt_cache_breakpoint"] != {"mode": "explicit"}:
                    raise ProviderFault("unsupported_anthropic_cache_breakpoint")
                block["cache_control"] = {"type": "ephemeral", "ttl": "5m"}
            if message.get("is_error"):
                block["is_error"] = True
            if (
                messages
                and messages[-1]["role"] == "user"
                and all(piece.get("type") == "tool_result" for piece in messages[-1]["content"])
            ):
                messages[-1]["content"].append(block)
            else:
                messages.append(dict(role="user", content=[block]))
            continue
        if message["role"] != "user":
            raise ProviderFault("unsupported_anthropic_input")
        messages.append(
            {
                "role": "user",
                "content": [_text_block(piece) for piece in message["content"]],
            }
        )
    result = dict(
        model=body["model"],
        system=body["instructions"],
        messages=messages,
        thinking={"type": "adaptive"},
        output_config=dict(
            effort=body["reasoning"]["effort"],
            format=dict(
                type="json_schema", schema=_native_schema(body["text"]["format"]["schema"], output=True)
            ),
        ),
    )
    if body.get("tools"):
        if body["tool_choice"] not in ("auto", "none") or body.get("parallel_tool_calls"):
            raise ProviderFault("unsupported_anthropic_tool_configuration")
        result["tools"] = [
            dict(
                name=t["name"],
                description=t["description"],
                input_schema=_native_schema(t["parameters"], output=False),
                **({"strict": t["strict"]} if "strict" in t else {}),
            )
            for t in body["tools"]
        ]
        result["tool_choice"] = (
            dict(type="auto", disable_parallel_tool_use=True)
            if body["tool_choice"] == "auto"
            else dict(type="none")
        )
    if not counting:
        result.update(max_tokens=body["max_output_tokens"], stream=False)
    if body.get("metadata", {}).get("podgen_prompt_cache_layout") == "research-role-prefix/2":
        from podgen.runtime.evaluation.research_cache_layout import validate_boundaries

        validate_boundaries(result)
    return result


def _usage_count(data, name, *, optional=False):
    value = data.get(name, 0 if optional else None)
    if type(value) is not int or value < 0:
        raise ValueError("invalid provider usage")
    return value


def normalize_message(message, request):
    if (
        message["model"] != request["model"]
        or message["role"] != "assistant"
        or message["type"] != "message"
        or not isinstance(message["id"], str)
        or not message["id"]
    ):
        raise ValueError("invalid message identity")
    parts = []
    for block in message["content"]:
        kind = block["type"]
        if kind == "text" and block["text"]:
            parts.append(
                dict(
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[dict(type="output_text", text=block["text"])],
                )
            )
        elif kind == "tool_use":
            parts.append(
                dict(
                    type="function_call",
                    name=block["name"],
                    call_id=block["id"],
                    status="completed",
                    arguments=json.dumps(block["input"], ensure_ascii=False),
                )
            )
        elif kind not in ("text", "thinking", "redacted_thinking"):
            raise ValueError("unexpected content block")
    stop = message["stop_reason"]
    if (
        stop == "tool_use"
        and not any(row["type"] == "function_call" for row in parts)
        or stop == "end_turn"
        and any(row["type"] == "function_call" for row in parts)
    ):
        raise ValueError("inconsistent tool stop reason")
    if stop == "refusal":
        parts = [dict(type="message", role="assistant", status="completed", content=[dict(type="refusal")])]
    usage = message["usage"]
    cached = _usage_count(usage, "cache_read_input_tokens", optional=True)
    written = _usage_count(usage, "cache_creation_input_tokens", optional=True)
    accounting = dict(
        input_tokens=_usage_count(usage, "input_tokens") + cached + written,
        output_tokens=_usage_count(usage, "output_tokens"),
    )
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        accounting["input_tokens_details"] = dict(cached_tokens=cached, cache_write_tokens=written)
    result = dict(
        id="resp_" + message["id"],
        model=message["model"],
        metadata=request["metadata"],
        status="completed" if stop in ("end_turn", "tool_use", "refusal") else "incomplete",
        output=parts,
        usage=accounting,
    )
    if request.get("tools"):
        result["anthropic_content"] = deepcopy(message["content"])
    return result
