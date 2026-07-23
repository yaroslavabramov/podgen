from __future__ import annotations

from hashlib import sha256
from typing import Any

from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_state import (
    READ_RECEIPT_DELIVERY_DELIVERED,
    READ_RECEIPT_DELIVERY_PENDING,
    now as workspace_now,
    source_read_receipt_is_delivered,
)


SEMANTIC_REVIEW_DELIVERIES_FIELD = "source_triage_semantic_review_deliveries"
QUESTION_WORKBENCH_DELIVERIES_FIELD = "source_triage_question_workbench_deliveries"
FINDING_CATALOG_DELIVERIES_FIELD = "source_triage_finding_catalog_deliveries"
SOURCE_STRUCTURE_PAGES_FIELD = "source_structure_pages"


def fit_staged_model_page(
    payload: dict[str, Any],
    *,
    requested_limit: int,
    receipt_field: str,
    receipt_prefix: str,
    error_code: str = "triage_workspace_item_exceeds_model_read_contract",
) -> dict[str, Any]:
    """Fit a page after reserving the exact durable receipt envelope."""

    placeholder = f"{receipt_prefix}:{'0' * 24}"
    return fit_model_page(
        {**payload, receipt_field: placeholder},
        requested_limit=requested_limit,
        error_code=error_code,
    )


def semantic_review_receipt_ref(
    *,
    set_id: str,
    snapshot_id: str,
    cursor: int,
    returned_count: int,
) -> str:
    identity = "\x1f".join((set_id, snapshot_id, str(cursor), str(returned_count)))
    return f"review_receipt:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def question_workbench_receipt_ref(
    *,
    set_id: str,
    question_id: str,
    snapshot_id: str,
    cursor: int,
    returned_count: int,
) -> str:
    identity = "\x1f".join(
        (set_id, question_id, snapshot_id, str(cursor), str(returned_count))
    )
    return f"workbench_receipt:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def finding_catalog_receipt_ref(
    *,
    set_id: str,
    snapshot_id: str,
    cursor: int,
    returned_count: int,
) -> str:
    identity = "\x1f".join((set_id, snapshot_id, str(cursor), str(returned_count)))
    return f"finding_catalog_receipt:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def source_structure_receipt_ref(
    *,
    set_id: str,
    source_ref: str,
    source_version_id: str,
    content_sha256: str,
    cursor: str,
    returned_count: int,
) -> str:
    identity = "\x1f".join(
        (
            set_id,
            source_ref,
            source_version_id,
            content_sha256,
            cursor,
            str(returned_count),
        )
    )
    return f"structure_receipt:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def stage_semantic_review_page(
    workspace: dict[str, Any],
    *,
    source_set: dict[str, Any],
    payload: dict[str, Any],
    receipt_ref: str,
) -> None:
    page = dict(payload.get("page") or {})
    record = {
        "receipt_ref": receipt_ref,
        "set_id": str(source_set.get("set_id") or ""),
        "review_snapshot_id": str(payload.get("review_snapshot_id") or ""),
        "cursor": str(page.get("cursor") or "0"),
        "returned_count": int(page.get("returned_count") or 0),
        "total_count": int(page.get("total_count") or 0),
        "delivery_status": READ_RECEIPT_DELIVERY_PENDING,
        "created_at": workspace_now(),
    }
    workspace[SEMANTIC_REVIEW_DELIVERIES_FIELD] = _replace_by_receipt_ref(
        workspace.get(SEMANTIC_REVIEW_DELIVERIES_FIELD),
        record,
    )


def stage_question_workbench_page(
    workspace: dict[str, Any],
    *,
    source_set: dict[str, Any],
    question_id: str,
    payload: dict[str, Any],
    receipt_ref: str,
) -> None:
    page = dict(payload.get("page") or {})
    record = {
        "receipt_ref": receipt_ref,
        "set_id": str(source_set.get("set_id") or ""),
        "question_id": question_id,
        "workbench_snapshot_id": str(payload.get("workbench_snapshot_id") or ""),
        "cursor": str(page.get("cursor") or "0"),
        "returned_count": int(page.get("returned_count") or 0),
        "total_count": int(page.get("total_count") or 0),
        "delivery_status": READ_RECEIPT_DELIVERY_PENDING,
        "created_at": workspace_now(),
    }
    workspace[QUESTION_WORKBENCH_DELIVERIES_FIELD] = _replace_by_receipt_ref(
        workspace.get(QUESTION_WORKBENCH_DELIVERIES_FIELD),
        record,
    )


def stage_finding_catalog_page(
    workspace: dict[str, Any],
    *,
    source_set: dict[str, Any],
    payload: dict[str, Any],
    receipt_ref: str,
) -> None:
    page = dict(payload.get("page") or {})
    record = {
        "receipt_ref": receipt_ref,
        "set_id": str(source_set.get("set_id") or ""),
        "finding_catalog_snapshot_id": str(payload.get("finding_catalog_snapshot_id") or ""),
        "cursor": str(page.get("cursor") or "0"),
        "returned_count": int(page.get("returned_count") or 0),
        "total_count": int(page.get("total_count") or 0),
        "delivery_status": READ_RECEIPT_DELIVERY_PENDING,
        "created_at": workspace_now(),
    }
    workspace[FINDING_CATALOG_DELIVERIES_FIELD] = _replace_by_receipt_ref(
        workspace.get(FINDING_CATALOG_DELIVERIES_FIELD),
        record,
    )


def stage_source_structure_page(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    source_version_id: str,
    content_sha256: str,
    payload: dict[str, Any],
    receipt_ref: str,
) -> None:
    record = {
        "receipt_ref": receipt_ref,
        "set_id": set_id,
        "source_ref": source_ref,
        "source_version_id": source_version_id,
        "content_sha256": content_sha256,
        "cursor": str(payload.get("cursor") or "0"),
        "next_cursor": str(payload.get("next_cursor") or ""),
        "total_count": int(payload.get("total_count") or 0),
        "returned_count": int(payload.get("returned_count") or 0),
        "omitted_count": int(payload.get("omitted_count") or 0),
        "total_chars": int(payload.get("total_chars") or 0),
        "counts": dict(payload.get("counts") or {}),
        "items": [dict(item) for item in payload.get("items") or [] if isinstance(item, dict)],
        "delivery_status": READ_RECEIPT_DELIVERY_PENDING,
        "created_at": workspace_now(),
    }
    workspace[SOURCE_STRUCTURE_PAGES_FIELD] = _replace_by_receipt_ref(
        workspace.get(SOURCE_STRUCTURE_PAGES_FIELD),
        record,
    )


async def mark_runtime_delivery_receipts_delivered(
    db: Any,
    *,
    generation_run_id: Any,
    receipt_refs: list[str],
) -> int:
    """Acknowledge durable model-visible payloads only after the next provider response."""

    wanted = {str(value or "").strip() for value in receipt_refs if str(value or "").strip()}
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
        if receipt_ref not in wanted:
            continue
        acknowledged.add(receipt_ref)
        if not source_read_receipt_is_delivered(receipt):
            receipt["delivery_status"] = READ_RECEIPT_DELIVERY_DELIVERED
            receipt["delivered_at"] = delivered_at
            changed = True

    for record in workspace.get(SOURCE_STRUCTURE_PAGES_FIELD) or []:
        if not isinstance(record, dict):
            continue
        receipt_ref = str(record.get("receipt_ref") or "")
        if receipt_ref not in wanted:
            continue
        acknowledged.add(receipt_ref)
        if str(record.get("delivery_status") or "") != READ_RECEIPT_DELIVERY_DELIVERED:
            record["delivery_status"] = READ_RECEIPT_DELIVERY_DELIVERED
            record["delivered_at"] = delivered_at
            changed = True

    for field in (
        QUESTION_WORKBENCH_DELIVERIES_FIELD,
        FINDING_CATALOG_DELIVERIES_FIELD,
    ):
        for record in workspace.get(field) or []:
            if not isinstance(record, dict):
                continue
            receipt_ref = str(record.get("receipt_ref") or "")
            if receipt_ref not in wanted:
                continue
            acknowledged.add(receipt_ref)
            if str(record.get("delivery_status") or "") != READ_RECEIPT_DELIVERY_DELIVERED:
                record["delivery_status"] = READ_RECEIPT_DELIVERY_DELIVERED
                record["delivered_at"] = delivered_at
                changed = True

    from app.generation_v2.source_triage_set_review import record_semantic_review_page

    consumed_review_refs: set[str] = set()
    for record in workspace.get(SEMANTIC_REVIEW_DELIVERIES_FIELD) or []:
        if not isinstance(record, dict):
            continue
        receipt_ref = str(record.get("receipt_ref") or "")
        if receipt_ref not in wanted:
            continue
        acknowledged.add(receipt_ref)
        consumed_review_refs.add(receipt_ref)
        if str(record.get("delivery_status") or "") != READ_RECEIPT_DELIVERY_DELIVERED:
            record["delivery_status"] = READ_RECEIPT_DELIVERY_DELIVERED
            record["delivered_at"] = delivered_at
            changed = True
        source_set = next(
            (
                item
                for item in workspace.get("source_sets") or []
                if isinstance(item, dict)
                and str(item.get("set_id") or "") == str(record.get("set_id") or "")
            ),
            None,
        )
        if source_set is None:
            continue
        previous_review = dict(
            (workspace.get("source_triage_set_semantic_reviews") or {}).get(
                str(source_set.get("set_id") or "")
            )
            or {}
        )
        record_semantic_review_page(
            workspace,
            source_set,
            {
                "accepted": True,
                "review_snapshot_id": str(record.get("review_snapshot_id") or ""),
                "page": {
                    "cursor": str(record.get("cursor") or "0"),
                    "returned_count": int(record.get("returned_count") or 0),
                    "total_count": int(record.get("total_count") or 0),
                },
            },
        )
        current_review = dict(
            (workspace.get("source_triage_set_semantic_reviews") or {}).get(
                str(source_set.get("set_id") or "")
            )
            or {}
        )
        changed = changed or current_review != previous_review

    if consumed_review_refs:
        workspace[SEMANTIC_REVIEW_DELIVERIES_FIELD] = [
            record
            for record in workspace.get(SEMANTIC_REVIEW_DELIVERIES_FIELD) or []
            if not isinstance(record, dict)
            or str(record.get("receipt_ref") or "") not in consumed_review_refs
        ]
        changed = True

    if changed:
        await save_source_triage_workspace(
            db,
            generation_run_id=generation_run_id,
            workspace=workspace,
        )
    return len(acknowledged)


def source_structure_coverage(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> dict[str, Any]:
    pages = [
        item
        for item in workspace.get(SOURCE_STRUCTURE_PAGES_FIELD) or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "") == source_ref
        and str(item.get("delivery_status") or "") == READ_RECEIPT_DELIVERY_DELIVERED
    ]
    if not pages:
        return {}
    pages.sort(key=lambda item: int(str(item.get("cursor") or "0")))
    total_count = max((int(item.get("total_count") or 0) for item in pages), default=0)
    read_indexes: set[int] = set()
    items_by_index: dict[int, dict[str, Any]] = {}
    for page in pages:
        cursor = int(str(page.get("cursor") or "0"))
        for offset, item in enumerate(page.get("items") or []):
            if not isinstance(item, dict):
                continue
            index = cursor + offset
            read_indexes.add(index)
            items_by_index[index] = dict(item)
    unread_indexes = [index for index in range(total_count) if index not in read_indexes]
    latest = pages[-1]
    return {
        "source_ref": source_ref,
        "total_count": total_count,
        "items": [items_by_index[index] for index in sorted(items_by_index)],
        "returned_count": len(items_by_index),
        "omitted_count": len(unread_indexes),
        "complete": not unread_indexes,
        "next_unread_cursor": str(unread_indexes[0]) if unread_indexes else "",
        "counts": dict(latest.get("counts") or {}),
        "total_chars": int(latest.get("total_chars") or 0),
        "delivered_page_count": len(pages),
    }


def question_workbench_is_fully_delivered(
    workspace: dict[str, Any],
    *,
    set_id: str,
    question_id: str,
    snapshot_id: str,
) -> bool:
    return question_workbench_delivery_state(
        workspace,
        set_id=set_id,
        question_id=question_id,
        snapshot_id=snapshot_id,
    )["complete"]


def question_workbench_delivery_state(
    workspace: dict[str, Any],
    *,
    set_id: str,
    question_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    deliveries = [
        item
        for item in workspace.get(QUESTION_WORKBENCH_DELIVERIES_FIELD) or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("question_id") or "") == question_id
        and str(item.get("workbench_snapshot_id") or "") == snapshot_id
        and str(item.get("delivery_status") or "") == READ_RECEIPT_DELIVERY_DELIVERED
    ]
    return _paged_delivery_state(deliveries)


def finding_catalog_is_fully_delivered(
    workspace: dict[str, Any],
    *,
    set_id: str,
    snapshot_id: str,
) -> bool:
    return finding_catalog_delivery_state(
        workspace,
        set_id=set_id,
        snapshot_id=snapshot_id,
    )["complete"]


def finding_catalog_delivery_state(
    workspace: dict[str, Any],
    *,
    set_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    deliveries = [
        item
        for item in workspace.get(FINDING_CATALOG_DELIVERIES_FIELD) or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("finding_catalog_snapshot_id") or "") == snapshot_id
        and str(item.get("delivery_status") or "") == READ_RECEIPT_DELIVERY_DELIVERED
    ]
    return _paged_delivery_state(deliveries)


def _paged_delivery_state(deliveries: list[dict[str, Any]]) -> dict[str, Any]:
    if not deliveries:
        return {
            "complete": False,
            "total_count": 0,
            "delivered_count": 0,
            "first_unread_cursor": "0",
        }
    total_count = max(int(item.get("total_count") or 0) for item in deliveries)
    delivered_indexes: set[int] = set()
    for item in deliveries:
        cursor = int(str(item.get("cursor") or "0"))
        returned = int(item.get("returned_count") or 0)
        delivered_indexes.update(
            index
            for index in range(cursor, cursor + returned)
            if 0 <= index < total_count
        )
    unread_indexes = sorted(set(range(total_count)) - delivered_indexes)
    return {
        "complete": not unread_indexes,
        "total_count": total_count,
        "delivered_count": len(delivered_indexes),
        "first_unread_cursor": str(unread_indexes[0]) if unread_indexes else "",
    }


def _replace_by_receipt_ref(value: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
    receipt_ref = str(record.get("receipt_ref") or "")
    items = [dict(item) for item in value or [] if isinstance(item, dict)]
    previous = next((item for item in items if str(item.get("receipt_ref") or "") == receipt_ref), None)
    if previous and str(previous.get("delivery_status") or "") == READ_RECEIPT_DELIVERY_DELIVERED:
        return items
    return [
        *[item for item in items if str(item.get("receipt_ref") or "") != receipt_ref],
        record,
    ]


__all__ = [
    "FINDING_CATALOG_DELIVERIES_FIELD",
    "QUESTION_WORKBENCH_DELIVERIES_FIELD",
    "SEMANTIC_REVIEW_DELIVERIES_FIELD",
    "SOURCE_STRUCTURE_PAGES_FIELD",
    "finding_catalog_delivery_state",
    "finding_catalog_is_fully_delivered",
    "finding_catalog_receipt_ref",
    "fit_staged_model_page",
    "mark_runtime_delivery_receipts_delivered",
    "question_workbench_delivery_state",
    "question_workbench_is_fully_delivered",
    "question_workbench_receipt_ref",
    "semantic_review_receipt_ref",
    "source_structure_coverage",
    "source_structure_receipt_ref",
    "stage_finding_catalog_page",
    "stage_semantic_review_page",
    "stage_question_workbench_page",
    "stage_source_structure_page",
]
