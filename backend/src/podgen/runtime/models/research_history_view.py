"""Lossless task history, separate from the provider transcript used for replay."""

import json

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.validation import load_unique_json

VERSION = "research-work-history/1"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def decoded(value):
    if not isinstance(value, str):
        return value
    try:
        return load_unique_json(value)
    except ValueError:
        # Invalid tool arguments are part of the work history too. Keep them
        # once, verbatim, so the original error can still be understood.
        return value


def tool_result(data):
    return dict(
        action=data["name"],
        arguments=data["arguments"] if data["arguments"] is not None else data["raw_arguments"],
        result=data["result"],
    )


def conversation(items):
    """Retain messages/actions in order; use wire IDs only to join their results."""
    entries, calls = [], {}

    def action(identifier, name, arguments):
        if identifier in calls:
            raise ContractError("duplicate_history_action")
        entry = dict(action=name, arguments=decoded(arguments))
        calls[identifier] = entry
        entries.append(entry)

    def message(role, content):
        if isinstance(content, str):
            entries.append(dict(speaker=role, text=content))
            return
        for block in content:
            kind = block.get("type")
            if kind in {"text", "input_text", "output_text"}:
                entries.append(dict(speaker=role, text=block["text"]))
            elif kind == "refusal":
                entries.append(dict(speaker=role, text=block["refusal"]))
            elif kind == "tool_use":
                action(block["id"], block["name"], block["input"])
            elif kind not in {"thinking", "redacted_thinking"}:
                raise ContractError("unsupported_history_content")

    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            action(item["call_id"], item["name"], item["arguments"])
        elif kind == "function_call_output":
            target = calls.get(item["call_id"])
            if target is None or "result" in target:
                raise ContractError("unbound_history_result")
            target["result"] = decoded(item["output"])
            if item.get("is_error"):
                target["failed"] = True
        elif kind == "anthropic_assistant":
            message("assistant", item["content"])
        elif kind == "message" or kind is None and "role" in item:
            message(item["role"], item["content"])
        elif kind != "reasoning":
            raise ContractError("unsupported_history_item")
    return entries
