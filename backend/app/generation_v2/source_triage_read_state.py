from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from app.generation_v2.source_triage_workspace_state import (
    source_read_coverage_ledger,
    source_read_receipt_is_delivered,
)


def canonical_source_read_state(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    acknowledge_pending: bool = False,
) -> dict[str, Any]:
    """Return the stable, content-addressed reading state for one source."""

    receipts = [
        dict(item)
        for item in workspace.get("source_read_receipts") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "") == source_ref
        and source_read_receipt_is_delivered(item)
    ]
    ledger = next(
        (
            dict(item)
            for item in source_read_coverage_ledger(receipts, set_id=set_id)
            if str(item.get("source_ref") or "") == source_ref
        ),
        {
            "source_ref": source_ref,
            "total_chars": 0,
            "sequential_ranges": [],
            "sequential_chars_read": 0,
            "unread_char_count": 0,
            "eof_reached": False,
            "search_ranges": [],
            "search_queries": [],
            "structure": {},
            "completion_is_model_judgment": True,
        },
    )
    structure_pages = [
        dict(item)
        for item in workspace.get("source_structure_pages") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "") == source_ref
        and str(item.get("delivery_status") or "") == "delivered"
    ]
    structures = [
        dict(item.get("structure") or {})
        for item in receipts
        if isinstance(item.get("structure"), dict)
    ]
    structures.extend(_structure_page_view(item) for item in structure_pages)
    ledger["structure"] = merge_source_structures(structures, source_ref=source_ref)
    pending_exact_reads = _pending_read_items(
        workspace,
        field="pending_exact_reads",
        set_id=set_id,
        source_ref=source_ref,
    )
    pending_comparison_reads = _pending_read_items(
        workspace,
        field="pending_comparison_reads",
        set_id=set_id,
        source_ref=source_ref,
    )
    return {
        "source_ref": source_ref,
        "delivered_receipts": sorted(
            {
                _stable_json(_canonical_receipt(item)): _canonical_receipt(item)
                for item in receipts
            }.values(),
            key=_stable_json,
        ),
        "coverage_ledger": ledger,
        "pending_exact_reads": [] if acknowledge_pending else pending_exact_reads,
        "pending_comparison_reads": [] if acknowledge_pending else pending_comparison_reads,
    }


def source_read_state_signature(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    acknowledge_pending: bool = False,
) -> str:
    state = canonical_source_read_state(
        workspace,
        set_id=set_id,
        source_ref=source_ref,
        acknowledge_pending=acknowledge_pending,
    )
    # Navigation method and query do not change the delivered source material.
    # The ledger remains available to the agent, while actuality is bound only
    # to content-addressed ranges, the merged structure, and unreconciled reads.
    signature_state = {
        "source_ref": state["source_ref"],
        "delivered_receipts": state["delivered_receipts"],
        "structure": dict(state.get("coverage_ledger") or {}).get("structure") or {},
        "pending_exact_reads": state["pending_exact_reads"],
        "pending_comparison_reads": state["pending_comparison_reads"],
    }
    return sha256(_stable_json(signature_state).encode("utf-8")).hexdigest()


def merge_source_structures(
    structures: list[dict[str, Any]],
    *,
    source_ref: str,
) -> dict[str, Any]:
    """Merge receipt and paged structure into one representation-independent view."""

    nonempty = [item for item in structures if isinstance(item, dict) and item]
    if not nonempty:
        return {}
    items_by_identity: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    total_count = 0
    total_chars = 0
    for structure in nonempty:
        returned_count = int(structure.get("returned_count") or 0)
        omitted_count = int(structure.get("omitted_count") or 0)
        total_count = max(
            total_count,
            int(structure.get("total_count") or 0),
            returned_count + omitted_count,
        )
        total_chars = max(total_chars, int(structure.get("total_chars") or 0))
        for key, value in dict(structure.get("counts") or {}).items():
            try:
                counts[str(key)] = max(counts.get(str(key), 0), int(value or 0))
            except (TypeError, ValueError):
                continue
        for item in structure.get("items") or []:
            if not isinstance(item, dict):
                continue
            semantic_item = {
                key: item.get(key)
                for key in (
                    "kind",
                    "label_preview",
                    "label_chars",
                    "label_omitted_chars",
                    "start_offset",
                    "end_offset",
                    "section_chars",
                    "navigation_window_range",
                    "navigation_window_omitted_chars",
                    "window_ref",
                )
                if key in item
            }
            items_by_identity[_stable_json(semantic_item)] = semantic_item
    items = sorted(
        items_by_identity.values(),
        key=lambda item: (
            int(item.get("start_offset") or 0),
            str(item.get("kind") or ""),
            str(item.get("label_preview") or ""),
        ),
    )
    total_count = max(total_count, len(items))
    complete = total_count > 0 and len(items) >= total_count
    return {
        "source_ref": source_ref,
        "total_count": total_count,
        "items": items,
        "returned_count": len(items),
        "omitted_count": max(0, total_count - len(items)),
        "complete": complete,
        "next_unread_cursor": str(len(items)) if not complete else "",
        "counts": counts,
        "total_chars": total_chars,
    }


def _canonical_receipt(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "source_version_id": str(item.get("source_version_id") or ""),
            "content_sha256": str(item.get("content_sha256") or ""),
            "start_offset": int(item.get("start_offset") or 0),
            "end_offset": int(item.get("end_offset") or 0),
            "total_chars": int(item.get("total_chars") or 0),
        }.items()
        if value not in {"", None}
    }


def _structure_page_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_ref": str(item.get("source_ref") or ""),
        "total_count": int(item.get("total_count") or 0),
        "items": [dict(value) for value in item.get("items") or [] if isinstance(value, dict)],
        "counts": dict(item.get("counts") or {}),
        "total_chars": int(item.get("total_chars") or 0),
    }


def _pending_read_items(
    workspace: dict[str, Any],
    *,
    field: str,
    set_id: str,
    source_ref: str,
) -> list[dict[str, Any]]:
    source_set = next(
        (
            item
            for item in workspace.get("source_sets") or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ),
        {},
    )
    return sorted(
        (
            {
                "source_ref": source_ref,
                "evidence_ids": sorted(
                    {str(value) for value in item.get("evidence_ids") or [] if str(value)}
                ),
            }
            for item in source_set.get(field) or []
            if isinstance(item, dict)
            and str(item.get("source_ref") or "") == source_ref
        ),
        key=_stable_json,
    )


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "canonical_source_read_state",
    "merge_source_structures",
    "source_read_state_signature",
]
