from __future__ import annotations

from typing import Any

from app.services.llm import _openai_response_model
from app.generation_v2.runtime_response_status import (
    response_error,
    response_incomplete_reason,
    response_output_statuses,
)


def tool_trace_name(tool: dict[str, Any]) -> str:
    if tool.get("type") == "function":
        return str(tool.get("name") or "function")
    return str(tool.get("type") or tool.get("name") or "unknown")


def safe_tool_dump_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in str(name or "tool"))
    return safe[:80] or "tool"


def tool_policy_payload(policy: Any) -> dict[str, Any]:
    return {
        "file_search": policy.file_search,
        "web_search": policy.web_search,
        "local_tools": policy.local_tools,
        "max_file_search_results": policy.max_file_search_results,
        "parallel_local_tool_calls": policy.parallel_local_tool_calls,
    }


def redact_runtime_file_ids(value: Any) -> Any:
    file_ids: set[str] = set()

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key == "file_id" and isinstance(nested, str) and nested:
                    file_ids.add(nested)
                collect(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                collect(nested)

    collect(value)

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "[redacted]" if key == "file_id" else redact(nested)
                for key, nested in item.items()
            }
        if isinstance(item, list):
            return [redact(nested) for nested in item]
        if isinstance(item, tuple):
            return [redact(nested) for nested in item]
        if isinstance(item, str):
            redacted = item
            for file_id in file_ids:
                redacted = redacted.replace(file_id, "[redacted]")
            return redacted
        return item

    return redact(value)

def runtime_debug_payload(
    *,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    round_idx: int,
    prompt_hash: str,
    system: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    tools: list[dict[str, Any]],
    policy: Any,
    input_payload: Any,
    response: Any,
    usage: dict[str, Any],
    file_search_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    function_calls: list[dict[str, Any]],
    hosted_followup: dict[str, Any] | None,
    max_output_tokens: int | None = None,
    server_compaction_threshold: int | None = None,
) -> dict[str, Any]:
    return redact_runtime_file_ids({
        "run_id": str(generation_run_id),
        "phase": phase,
        "agent_name": agent_name,
        "round": round_idx + 1,
        "prompt_hash": prompt_hash,
        "system": system,
        "prompt": prompt,
        "schema_name": schema_name,
        "schema": schema,
        "tools": [tool_trace_name(tool) for tool in tools],
        "tool_policy": tool_policy_payload(policy),
        "input_payload": input_payload,
        "max_output_tokens": max_output_tokens,
        "server_compaction_threshold": server_compaction_threshold,
        "response": {
            "id": getattr(response, "id", None),
            "model": _openai_response_model(response),
            "status": getattr(response, "status", None),
            "incomplete_reason": response_incomplete_reason(response),
            "provider_error": response_error(response),
            "output_item_statuses": response_output_statuses(response),
            "output_text": getattr(response, "output_text", "") or "",
        },
        "usage": usage,
        "file_search_calls": file_search_calls,
        "web_search_calls": web_search_calls,
        "function_calls": function_calls,
        "hosted_followup": hosted_followup,
    })
