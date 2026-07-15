from __future__ import annotations

from typing import Any


def _response_field(response: Any, field: str) -> Any:
    if isinstance(response, dict):
        return response.get(field)
    return getattr(response, field, None)


def response_status(response: Any) -> str:
    return str(_response_field(response, "status") or "unknown")


def response_incomplete_reason(response: Any) -> str | None:
    details = _response_field(response, "incomplete_details")
    if isinstance(details, dict):
        reason = details.get("reason")
    else:
        reason = getattr(details, "reason", None)
    return str(reason) if reason else None


def response_error(response: Any) -> dict[str, str]:
    error = _response_field(response, "error")
    if error is None:
        return {}
    fields: dict[str, str] = {}
    for name in ("type", "code", "message", "param"):
        value = error.get(name) if isinstance(error, dict) else getattr(error, name, None)
        if value is not None and str(value).strip():
            fields[name] = str(value)
    return fields


def response_output_statuses(response: Any, *, limit: int = 24) -> list[dict[str, str]]:
    output = _response_field(response, "output") or []
    statuses: list[dict[str, str]] = []
    for item in list(output)[:limit]:
        row: dict[str, str] = {}
        for name in ("type", "id", "call_id", "name", "status"):
            value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
            if value is not None and str(value).strip():
                row[name] = str(value)
        if row:
            statuses.append(row)
    return statuses


def require_completed_response(response: Any, *, operation: str) -> None:
    status = response_status(response)
    if status == "completed":
        return
    reason = response_incomplete_reason(response)
    error = response_error(response)
    detail = reason or error.get("message") or error.get("code")
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"{operation} returned provider response status {status}{suffix}")


def output_continuation_message() -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": (
                "The previous response exhausted its output budget before completing the workflow. "
                "Continue from the existing response chain and durable tool state. Do not repeat prior analysis. "
                "Call the next required persistence tool, repair a rejected tool call, or call the terminal tool now. "
                "If no terminal tool applies, return the required structured result."
            ),
        }
    ]


__all__ = [
    "output_continuation_message",
    "require_completed_response",
    "response_error",
    "response_incomplete_reason",
    "response_output_statuses",
    "response_status",
]
