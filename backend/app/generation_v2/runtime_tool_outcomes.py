from __future__ import annotations

from typing import Any


TOOL_OUTCOME_CONTROL = "control"
TOOL_OUTCOME_CONSTRAINT = "constraint"
TOOL_OUTCOME_MISS = "miss"
TOOL_OUTCOME_RUNTIME_ERROR = "runtime_error"
TOOL_OUTCOME_VALIDATION_ERROR = "validation_error"
TOOL_OUTCOME_TOOL_ERROR = TOOL_OUTCOME_VALIDATION_ERROR
TOOL_OUTCOME_SUCCESS = "success"

_MISS_ERRORS = {
    "locator_not_found",
    "no readable source version in this run corpus view.",
    "source version has no readable local text window.",
}
_CONSTRAINT_ERRORS = {
    "evidence_too_large_use_source_window",
    "triage_workspace_item_exceeds_model_read_contract",
    "workspace_item_exceeds_model_read_contract",
}
_READ_PAYLOAD_FIELDS = ("results", "evidence", "windows", "items", "sources")


def _has_explicit_empty_read_payload(payload: dict[str, Any]) -> bool:
    present_values = [payload[field] for field in _READ_PAYLOAD_FIELDS if field in payload]
    if "comparisons" in payload:
        comparisons = payload.get("comparisons")
        if isinstance(comparisons, list):
            present_values.append(
                [
                    item
                    for item in comparisons
                    if not isinstance(item, dict) or bool(item.get("evidence"))
                ]
            )
        else:
            present_values.append(comparisons)
    return bool(present_values) and not any(bool(value) for value in present_values)


def tool_output_outcome_kind(output: dict[str, Any] | None) -> str:
    """Classify tool traces without treating workflow control as a model error."""
    payload = output if isinstance(output, dict) else {}
    if payload.get("outcome_kind") == TOOL_OUTCOME_CONTROL:
        return TOOL_OUTCOME_CONTROL
    if payload.get("_runtime_error"):
        return TOOL_OUTCOME_RUNTIME_ERROR
    error = str(payload.get("error") or "").strip().lower()
    if payload.get("found") is False and (not error or error in _MISS_ERRORS):
        return TOOL_OUTCOME_MISS
    if error in _CONSTRAINT_ERRORS:
        return TOOL_OUTCOME_CONSTRAINT
    if payload.get("accepted") is False or payload.get("error"):
        return TOOL_OUTCOME_VALIDATION_ERROR
    if _has_explicit_empty_read_payload(payload):
        return TOOL_OUTCOME_MISS
    return TOOL_OUTCOME_SUCCESS


def is_control_tool_output(output: dict[str, Any] | None) -> bool:
    return tool_output_outcome_kind(output) == TOOL_OUTCOME_CONTROL


def successful_read_output(output: dict[str, Any] | None) -> bool:
    payload = output if isinstance(output, dict) else {}
    if not payload or tool_output_outcome_kind(payload) != TOOL_OUTCOME_SUCCESS or payload.get("found") is False:
        return False
    return bool(
        payload.get("accepted") is True
        or payload.get("found") is True
        or any(key in payload for key in ("results", "evidence", "windows", "items", "comparisons"))
    )


__all__ = [
    "TOOL_OUTCOME_CONTROL",
    "TOOL_OUTCOME_CONSTRAINT",
    "TOOL_OUTCOME_MISS",
    "TOOL_OUTCOME_RUNTIME_ERROR",
    "TOOL_OUTCOME_SUCCESS",
    "TOOL_OUTCOME_TOOL_ERROR",
    "TOOL_OUTCOME_VALIDATION_ERROR",
    "is_control_tool_output",
    "successful_read_output",
    "tool_output_outcome_kind",
]
