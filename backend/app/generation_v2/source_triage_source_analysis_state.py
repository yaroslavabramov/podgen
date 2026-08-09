from __future__ import annotations

from typing import Any

from app.generation_v2.source_triage_workspace_state import READ_RECEIPT_DELIVERY_DELIVERED

SOURCE_MAPPING_TERMINAL_FAILURES_FIELD = "source_mapping_terminal_failures"


def delivered_source_read_receipts(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> list[dict[str, Any]]:
    return [
        item
        for item in workspace.get("source_read_receipts") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "") == source_ref
        and str(item.get("delivery_status") or "") == READ_RECEIPT_DELIVERY_DELIVERED
    ]


def has_delivered_source_read(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> bool:
    return bool(delivered_source_read_receipts(workspace, set_id=set_id, source_ref=source_ref))


def has_delivered_source_eof(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> bool:
    return any(
        bool(item.get("eof"))
        and str(item.get("read_kind") or "sequential") == "sequential"
        for item in delivered_source_read_receipts(workspace, set_id=set_id, source_ref=source_ref)
    )


def record_source_mapping_terminal_failure(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    error: str,
    reason: str = "",
    retryable: bool = False,
    created_at: str = "",
) -> None:
    if not set_id or not source_ref or not error:
        return
    failures = [
        item
        for item in workspace.get(SOURCE_MAPPING_TERMINAL_FAILURES_FIELD) or []
        if not (
            isinstance(item, dict)
            and str(item.get("set_id") or "") == set_id
            and str(item.get("source_ref") or "") == source_ref
        )
    ]
    failures.append(
        {
            "set_id": set_id,
            "source_ref": source_ref,
            "error": error,
            "reason": reason,
            "retryable": bool(retryable),
            "created_at": created_at,
        }
    )
    workspace[SOURCE_MAPPING_TERMINAL_FAILURES_FIELD] = failures


def clear_source_mapping_terminal_failure(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> None:
    if not set_id or not source_ref:
        return
    workspace[SOURCE_MAPPING_TERMINAL_FAILURES_FIELD] = [
        item
        for item in workspace.get(SOURCE_MAPPING_TERMINAL_FAILURES_FIELD) or []
        if not (
            isinstance(item, dict)
            and str(item.get("set_id") or "") == set_id
            and str(item.get("source_ref") or "") == source_ref
        )
    ]


def has_source_mapping_terminal_failure(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "") == source_ref
        and bool(str(item.get("error") or "").strip())
        and item.get("retryable") is not True
        for item in workspace.get(SOURCE_MAPPING_TERMINAL_FAILURES_FIELD) or []
    )


def has_source_representation_failure(
    inventory: Any,
    source_id: str,
    *,
    workspace: dict[str, Any] | None = None,
    set_id: str = "",
    source_ref: str = "",
) -> bool:
    if workspace is not None and set_id and source_ref and has_source_mapping_terminal_failure(
        workspace,
        set_id=set_id,
        source_ref=source_ref,
    ):
        return True
    card = next(
        (item for item in getattr(inventory, "source_cards", []) or [] if str(item.source_id) == source_id),
        None,
    )
    if card is None:
        return False
    ready_count = int(getattr(card, "ready_representation_count", 0) or 0)
    failed_count = int(getattr(card, "failed_representation_count", 0) or 0)
    coverage_status = str(getattr(card, "coverage_status", "") or "")
    return failed_count > 0 and (ready_count == 0 or coverage_status == "failed")


def source_read_complete_from_backend(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    exact_duplicate: bool = False,
    representation_failure: bool = False,
) -> bool:
    return (
        exact_duplicate
        or representation_failure
        or has_delivered_source_eof(workspace, set_id=set_id, source_ref=source_ref)
    )


__all__ = [
    "delivered_source_read_receipts",
    "clear_source_mapping_terminal_failure",
    "has_delivered_source_eof",
    "has_delivered_source_read",
    "has_source_mapping_terminal_failure",
    "has_source_representation_failure",
    "record_source_mapping_terminal_failure",
    "SOURCE_MAPPING_TERMINAL_FAILURES_FIELD",
    "source_read_complete_from_backend",
]
