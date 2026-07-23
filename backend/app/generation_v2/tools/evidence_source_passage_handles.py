from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any, Iterable

from app.generation_v2.source_triage_workspace_state import (
    source_read_receipt_is_delivered,
)


PASSAGE_HANDLES_FIELD = "passage_handles"
_HANDLE_RE = re.compile(r"^P:([^:]+):(\d+)$")
_ERROR_CANDIDATE_LIMIT = 8


def attach_source_passage_handles(
    workspace: dict[str, Any],
    *,
    receipt: dict[str, Any],
    passages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist short model handles and return a model-facing passage list."""

    set_id = str(receipt.get("set_id") or "")
    source_ref = str(receipt.get("source_ref") or "")
    canonical_to_handle, used_handles = _workspace_handle_maps(
        workspace,
        set_id=set_id,
        source_ref=source_ref,
    )
    next_index = _next_handle_index(used_handles, source_ref=source_ref)
    records: list[dict[str, Any]] = []
    model_passages: list[dict[str, Any]] = []
    for passage in passages:
        canonical_ref = str(passage.get("passage_ref") or "")
        handle = canonical_to_handle.get(canonical_ref)
        if not handle:
            while f"P:{source_ref}:{next_index}" in used_handles:
                next_index += 1
            handle = f"P:{source_ref}:{next_index}"
            next_index += 1
            used_handles.add(handle)
            canonical_to_handle[canonical_ref] = handle
        record = {
            "passage_ref": handle,
            "canonical_passage_ref": canonical_ref,
            "start_offset": int((passage.get("range") or {}).get("start_offset") or 0),
            "end_offset": int((passage.get("range") or {}).get("end_offset") or 0),
        }
        records.append(record)
        model_passages.append({**passage, "passage_ref": handle})
    receipt[PASSAGE_HANDLES_FIELD] = records
    return model_passages


def model_passage_refs_for_receipt(receipt: dict[str, Any]) -> list[str]:
    return [
        str(item.get("passage_ref") or "")
        for item in receipt.get(PASSAGE_HANDLES_FIELD) or []
        if isinstance(item, dict) and str(item.get("passage_ref") or "")
    ]


def accepted_passage_refs_for_receipt(receipt: dict[str, Any]) -> list[str]:
    handles = model_passage_refs_for_receipt(receipt)
    if handles:
        return handles
    return [str(value) for value in receipt.get("passage_refs") or [] if str(value)]


def delivered_passage_ref_map(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    allowed_read_kinds: set[str] | None = None,
) -> dict[str, str]:
    """Map every accepted model or legacy ref to its canonical signed ref."""

    resolved: dict[str, str] = {}
    for receipt in workspace.get("source_read_receipts") or []:
        if (
            not isinstance(receipt, dict)
            or str(receipt.get("set_id") or "") != set_id
            or str(receipt.get("source_ref") or "") != source_ref
            or not source_read_receipt_is_delivered(receipt)
            or (
                allowed_read_kinds is not None
                and _receipt_read_kind(receipt) not in allowed_read_kinds
            )
        ):
            continue
        canonical_refs = [
            str(value) for value in receipt.get("passage_refs") or [] if str(value)
        ]
        for canonical_ref in canonical_refs:
            resolved[canonical_ref] = canonical_ref
        for item in receipt.get(PASSAGE_HANDLES_FIELD) or []:
            if not isinstance(item, dict):
                continue
            handle = str(item.get("passage_ref") or "")
            canonical_ref = str(item.get("canonical_passage_ref") or "")
            if handle and canonical_ref in resolved:
                resolved[handle] = canonical_ref
    return resolved


def validate_delivered_passage_refs(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    passage_refs: Iterable[str],
    allowed_read_kinds: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    available_ref_map = delivered_passage_ref_map(
        workspace,
        set_id=set_id,
        source_ref=source_ref,
        allowed_read_kinds=allowed_read_kinds,
    )
    requested = list(
        dict.fromkeys(str(value) for value in passage_refs if str(value).strip())
    )
    all_ref_map = (
        delivered_passage_ref_map(
            workspace,
            set_id=set_id,
            source_ref=source_ref,
        )
        if allowed_read_kinds is not None
        else available_ref_map
    )
    navigation_only = [
        value
        for value in requested
        if value in all_ref_map and value not in available_ref_map
    ]
    if navigation_only:
        return available_ref_map, {
            "accepted": False,
            "error": "search_passage_requires_exact_read",
            "source_ref": source_ref,
            "navigation_only_passage_refs": navigation_only,
            "exact_read_candidates": _exact_read_candidates(
                workspace,
                set_id=set_id,
                source_ref=source_ref,
                passage_refs=navigation_only,
            ),
            "retry_policy": {
                "preserve_all_other_analysis_fields": True,
                "replace_only_navigation_passage_refs": True,
            },
            "next_action": (
                "Call read_source_window with one returned receipt_ref and max_chars up to 3000. Then retry the "
                "same complete source analysis: remove that search passage_ref and add the returned evidence_id "
                "to the same finding."
            ),
        }
    unavailable = [value for value in requested if value not in available_ref_map]
    if not unavailable:
        return available_ref_map, None
    return available_ref_map, {
        "accepted": False,
        "error": "passage_ref_not_delivered_to_current_set_agent",
        "source_ref": source_ref,
        "invalid_passage_refs": unavailable,
        **passage_ref_repair_payload(
            unavailable,
            available_ref_map=available_ref_map,
        ),
        "output_language": str(
            ((workspace.get("intent") or {}).get("language") or "")
        ),
        "retry_policy": {
            "preserve_all_other_analysis_fields": True,
            "replace_only_invalid_passage_refs": True,
            "do_not_search_again": True,
        },
        "next_action": (
            "Retry the same complete source analysis. Replace only each invalid passage_ref with a returned short "
            "replacement candidate; preserve every other finding and prose field."
        ),
    }


def _exact_read_candidates(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    passage_refs: Iterable[str],
) -> list[dict[str, Any]]:
    requested = {str(value) for value in passage_refs if str(value)}
    candidates: list[dict[str, Any]] = []
    for receipt in workspace.get("source_read_receipts") or []:
        if (
            not isinstance(receipt, dict)
            or str(receipt.get("set_id") or "") != set_id
            or str(receipt.get("source_ref") or "") != source_ref
            or str(receipt.get("read_kind") or "") != "search"
            or not source_read_receipt_is_delivered(receipt)
            or not requested.intersection(accepted_passage_refs_for_receipt(receipt))
        ):
            continue
        candidates.append(
            {
                "receipt_ref": str(receipt.get("receipt_ref") or ""),
                "window_ref": str(receipt.get("window_ref") or ""),
                "range": {
                    "start_offset": int(receipt.get("start_offset") or 0),
                    "end_offset": int(receipt.get("end_offset") or 0),
                },
            }
        )
        if len(candidates) >= 3:
            break
    return candidates


def _receipt_read_kind(receipt: dict[str, Any]) -> str:
    # Receipts created before read_kind was persisted came only from the
    # sequential reader, so preserving them as sequential is lossless.
    return str(receipt.get("read_kind") or "sequential")


def passage_ref_repair_payload(
    invalid_refs: Iterable[str],
    *,
    available_ref_map: dict[str, str],
) -> dict[str, Any]:
    """Return bounded, concrete replacements without asking for another read."""

    valid_handles = sorted(
        ref for ref in available_ref_map if ref.startswith("P:")
    )
    canonical_to_handle = {
        canonical: ref
        for ref, canonical in available_ref_map.items()
        if ref.startswith("P:")
    }
    candidates: list[dict[str, Any]] = []
    for invalid_ref in invalid_refs:
        ranked: list[tuple[float, str]] = []
        for valid_ref, canonical_ref in available_ref_map.items():
            candidate_handle = (
                valid_ref if valid_ref.startswith("P:") else canonical_to_handle.get(canonical_ref, valid_ref)
            )
            score = SequenceMatcher(None, invalid_ref, valid_ref).ratio()
            ranked.append((score, candidate_handle))
        unique_ranked = sorted(set(ranked), key=lambda item: (-item[0], item[1]))
        close = [value for score, value in unique_ranked if score >= 0.55][
            :_ERROR_CANDIDATE_LIMIT
        ]
        if not close:
            close = valid_handles[:_ERROR_CANDIDATE_LIMIT]
        candidates.append(
            {
                "invalid_passage_ref": invalid_ref,
                "replacement_candidates": close,
            }
        )
    return {
        "passage_ref_corrections": candidates,
        "available_passage_refs": valid_handles[:_ERROR_CANDIDATE_LIMIT],
        "available_passage_ref_count": len(valid_handles),
        "available_passage_ref_omitted_count": max(
            0, len(valid_handles) - _ERROR_CANDIDATE_LIMIT
        ),
    }


def _workspace_handle_maps(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> tuple[dict[str, str], set[str]]:
    canonical_to_handle: dict[str, str] = {}
    used_handles: set[str] = set()
    for receipt in workspace.get("source_read_receipts") or []:
        if (
            not isinstance(receipt, dict)
            or str(receipt.get("set_id") or "") != set_id
            or str(receipt.get("source_ref") or "") != source_ref
        ):
            continue
        for item in receipt.get(PASSAGE_HANDLES_FIELD) or []:
            if not isinstance(item, dict):
                continue
            handle = str(item.get("passage_ref") or "")
            canonical_ref = str(item.get("canonical_passage_ref") or "")
            if handle and canonical_ref:
                canonical_to_handle.setdefault(canonical_ref, handle)
                used_handles.add(handle)
    return canonical_to_handle, used_handles


def _next_handle_index(handles: set[str], *, source_ref: str) -> int:
    indexes = [
        int(match.group(2))
        for handle in handles
        if (match := _HANDLE_RE.fullmatch(handle)) is not None
        and match.group(1) == source_ref
    ]
    return max(indexes, default=0) + 1


__all__ = [
    "PASSAGE_HANDLES_FIELD",
    "accepted_passage_refs_for_receipt",
    "attach_source_passage_handles",
    "delivered_passage_ref_map",
    "model_passage_refs_for_receipt",
    "passage_ref_repair_payload",
    "validate_delivered_passage_refs",
]
