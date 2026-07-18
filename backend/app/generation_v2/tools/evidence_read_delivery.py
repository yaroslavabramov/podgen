from __future__ import annotations

from typing import Any

from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_state import (
    READ_RECEIPT_DELIVERY_DELIVERED,
    READ_RECEIPT_DELIVERY_PENDING,
    now as workspace_now,
    source_read_receipt_delivery_status,
    source_read_receipt_is_delivered,
)


DELIVERABLE_SOURCE_READ_KINDS = frozenset(
    {"sequential", "search", "native_file_attachment"}
)


async def mark_source_read_receipts_delivered(
    db: Any,
    *,
    generation_run_id: Any,
    receipt_refs: list[str],
) -> int:
    """Acknowledge only durable source-read receipts named by a validated runtime directive."""

    wanted = {
        str(value or "").strip()
        for value in receipt_refs
        if str(value or "").strip()
    }
    if not wanted:
        return 0
    workspace = await load_source_triage_workspace(db, generation_run_id)
    if workspace is None:
        return 0
    acknowledged: set[str] = set()
    changed = False
    delivered_at = workspace_now()
    for receipt in workspace.get("source_read_receipts") or []:
        if not isinstance(receipt, dict):
            continue
        receipt_ref = str(receipt.get("receipt_ref") or "")
        if (
            receipt_ref not in wanted
            or str(receipt.get("read_kind") or "")
            not in DELIVERABLE_SOURCE_READ_KINDS
        ):
            continue
        acknowledged.add(receipt_ref)
        if not source_read_receipt_is_delivered(receipt):
            receipt["delivery_status"] = READ_RECEIPT_DELIVERY_DELIVERED
            receipt["delivered_at"] = delivered_at
            changed = True
    if changed:
        await save_source_triage_workspace(
            db,
            generation_run_id=generation_run_id,
            workspace=workspace,
        )
    return len(acknowledged)


__all__ = [
    "DELIVERABLE_SOURCE_READ_KINDS",
    "READ_RECEIPT_DELIVERY_DELIVERED",
    "READ_RECEIPT_DELIVERY_PENDING",
    "mark_source_read_receipts_delivered",
    "source_read_receipt_delivery_status",
    "source_read_receipt_is_delivered",
]
