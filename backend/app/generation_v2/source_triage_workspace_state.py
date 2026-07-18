from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

WORKSPACE_SCHEMA_VERSION = "source_triage_workspace.v3"
STRUCTURED_WORKSPACE_SCHEMA_VERSIONS = frozenset(
    {"source_triage_workspace.v2", WORKSPACE_SCHEMA_VERSION}
)
SOURCE_SET_KIND_COVERAGE = "coverage"
SOURCE_SET_KIND_ORIENTATION = "orientation"
READ_RECEIPT_DELIVERY_PENDING = "pending"
READ_RECEIPT_DELIVERY_DELIVERED = "delivered"


def source_read_receipt_delivery_status(receipt: dict[str, Any]) -> str:
    """Keep legacy text receipts readable; new receipts carry an explicit state."""

    status = str(receipt.get("delivery_status") or "").strip()
    if status in {
        READ_RECEIPT_DELIVERY_PENDING,
        READ_RECEIPT_DELIVERY_DELIVERED,
    }:
        return status
    if str(receipt.get("read_kind") or "") == "native_file_attachment":
        return READ_RECEIPT_DELIVERY_PENDING
    return READ_RECEIPT_DELIVERY_DELIVERED


def source_read_receipt_is_delivered(receipt: dict[str, Any]) -> bool:
    return (
        source_read_receipt_delivery_status(receipt)
        == READ_RECEIPT_DELIVERY_DELIVERED
    )


def is_structured_triage_workspace(workspace: dict[str, Any] | None) -> bool:
    return str((workspace or {}).get("schema_version") or "") in STRUCTURED_WORKSPACE_SCHEMA_VERSIONS


def normalize_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    workspace = dict(workspace or {})
    workspace.setdefault("schema_version", WORKSPACE_SCHEMA_VERSION)
    workspace.setdefault("_workspace_revision", 0)
    workspace.setdefault("source_order", [])
    workspace.setdefault("source_capsules", {})
    workspace.setdefault("planner_inspected_source_refs", [])
    workspace.setdefault("source_sets", [])
    workspace.setdefault("coverage_questions", [])
    workspace.setdefault("source_assessments", [])
    workspace.setdefault("source_findings", [])
    workspace.setdefault("source_read_receipts", [])
    workspace.setdefault("reading_notes", [])
    workspace.setdefault("coverage_outcomes", [])
    workspace.setdefault("coverage_syntheses", [])
    workspace.setdefault("source_triage_finalizer_reads", {})
    workspace.setdefault("triage_semantic_revision", 0)
    workspace.setdefault("processed_sets", [])
    workspace.setdefault("deferred_sources", [])
    workspace.setdefault("mapped_sources", [])
    workspace.setdefault("open_questions", [])
    workspace.setdefault("repair_operation", None)
    normalized_sets: list[dict[str, Any]] = []
    for raw_source_set in workspace.get("source_sets") or []:
        if not isinstance(raw_source_set, dict):
            continue
        source_set = dict(raw_source_set)
        # Source sets written before this additive contract were all planned as
        # question-bearing content sets.
        source_set.setdefault("set_kind", SOURCE_SET_KIND_COVERAGE)
        current_question_ids = dedupe(
            [str(item) for item in source_set.get("question_ids") or []]
        )
        stored_cross_ids = dedupe(
            [str(item) for item in source_set.get("cross_question_ids") or []]
        )
        if "planned_question_ids" in source_set:
            planned_question_ids = dedupe(
                [str(item) for item in source_set.get("planned_question_ids") or []]
            )
        else:
            # Older V2 sets predate explicit provenance for question links.
            # A stored cross list is authoritative; otherwise preserve all
            # existing links as planner-owned for compatibility.
            planned_question_ids = [
                question_id
                for question_id in current_question_ids
                if question_id not in set(stored_cross_ids)
            ]
        cross_question_ids = [
            question_id
            for question_id in current_question_ids
            if question_id not in set(planned_question_ids)
        ]
        source_set["planned_question_ids"] = planned_question_ids
        source_set["cross_question_ids"] = cross_question_ids
        source_set["question_ids"] = dedupe([*planned_question_ids, *cross_question_ids])
        source_set.setdefault("question_assignments", [])
        normalized_sets.append(source_set)
    workspace["source_sets"] = normalized_sets
    return workspace


def latest_source_read_receipts(
    receipts: list[Any],
    *,
    set_id: str = "",
) -> list[dict[str, Any]]:
    """Return each source's last sequential resume receipt, or its last other receipt."""

    latest_by_source: dict[str, dict[str, Any]] = {}
    source_order: list[str] = []
    for item in receipts:
        if not isinstance(item, dict):
            continue
        if not source_read_receipt_is_delivered(item):
            continue
        if set_id and str(item.get("set_id") or "") != set_id:
            continue
        source_ref = str(item.get("source_ref") or "")
        if not source_ref:
            continue
        if source_ref not in latest_by_source:
            source_order.append(source_ref)
        current = latest_by_source.get(source_ref)
        current_is_sequential = str((current or {}).get("read_kind") or "") == "sequential"
        item_is_sequential = str(item.get("read_kind") or "") == "sequential"
        if current is None or item_is_sequential or not current_is_sequential:
            latest_by_source[source_ref] = item
    return [latest_by_source[source_ref] for source_ref in source_order]


def uncheckpointed_source_read_replays(
    workspace: dict[str, Any],
    *,
    set_id: str,
) -> list[dict[str, Any]]:
    """Return exact replay actions for delivered reads whose meaning is not durable.

    A delivery receipt proves that one provider turn received material. It does
    not prove that a later worker can recover the model's understanding. A
    semantic reading note or completed source analysis is the durable boundary.
    """

    completed_analysis_times: dict[str, datetime] = {
        str(item.get("source_ref") or ""): updated_at
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "resolved") == "resolved"
        and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
        and (updated_at := parse_dt(item.get("updated_at"))) is not None
    }
    checkpointed_receipt_refs = {
        str(receipt_ref)
        for note in workspace.get("reading_notes") or []
        if isinstance(note, dict)
        and str(note.get("set_id") or "") == set_id
        and _reading_note_has_semantic_memory(note)
        for receipt_ref in note.get("receipt_refs") or []
        if str(receipt_ref)
    }
    receipts = [
        item
        for item in workspace.get("source_read_receipts") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and source_read_receipt_is_delivered(item)
    ]
    predecessor_by_range: dict[tuple[str, str, int], str] = {}
    for receipt in receipts:
        if str(receipt.get("read_kind") or "") != "sequential":
            continue
        try:
            end_offset = int(receipt.get("end_offset"))
        except (TypeError, ValueError):
            continue
        predecessor_by_range[
            (
                str(receipt.get("source_ref") or ""),
                str(receipt.get("source_version_id") or ""),
                end_offset,
            )
        ] = str(receipt.get("receipt_ref") or "")

    replays: list[dict[str, Any]] = []
    for receipt in receipts:
        receipt_ref = str(receipt.get("receipt_ref") or "")
        source_ref = str(receipt.get("source_ref") or "")
        read_kind = str(receipt.get("read_kind") or "")
        if (
            not receipt_ref
            or not source_ref
            or _analysis_is_newer_than_receipt(
                completed_analysis_times.get(source_ref),
                receipt,
            )
            or receipt_ref in checkpointed_receipt_refs
            or read_kind not in {"sequential", "native_file_attachment"}
        ):
            continue
        request = receipt.get("request") if isinstance(receipt.get("request"), dict) else {}
        if read_kind == "native_file_attachment":
            retry = {
                "tool": "open_source_file",
                "source_ref": source_ref,
                "reading_goal": str(request.get("reading_goal") or "").strip()
                or "Reread this native source because no durable reading note survived the prior delivery.",
            }
        else:
            try:
                start_offset = int(receipt.get("start_offset"))
                end_offset = int(receipt.get("end_offset"))
            except (TypeError, ValueError):
                continue
            requested_cursor = str(request.get("cursor") or "")
            replay_cursor = requested_cursor or predecessor_by_range.get(
                (
                    source_ref,
                    str(receipt.get("source_version_id") or ""),
                    start_offset,
                ),
                "",
            )
            try:
                requested_max_chars = int(request.get("max_chars"))
            except (TypeError, ValueError):
                requested_max_chars = max(1_000, min(20_000, end_offset - start_offset))
            retry = {
                "tool": "read_source",
                "source_ref": source_ref,
                "cursor": replay_cursor,
                "max_chars": max(1_000, min(20_000, requested_max_chars)),
            }
        replays.append(
            {
                "receipt_ref": receipt_ref,
                "source_ref": source_ref,
                "read_kind": read_kind,
                "start_offset": receipt.get("start_offset"),
                "end_offset": receipt.get("end_offset"),
                "retry": retry,
            }
        )
    return replays


def _reading_note_has_semantic_memory(note: dict[str, Any]) -> bool:
    return any(
        str(note.get(field) or "").strip()
        for field in (
            "what_was_read",
            "what_was_established",
            "boundaries",
            "unread_regions",
            "why_unread_is_safe",
        )
    ) or any(note.get(field) for field in ("question_links", "doubts", "next_checks"))


def _analysis_is_newer_than_receipt(
    analysis_time: datetime | None,
    receipt: dict[str, Any],
) -> bool:
    if analysis_time is None:
        return False
    receipt_time = parse_dt(receipt.get("delivered_at")) or parse_dt(receipt.get("created_at"))
    if receipt_time is None:
        return False
    return _utc_naive(analysis_time) >= _utc_naive(receipt_time)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def workspace_set(workspace: dict[str, Any], set_id: str) -> dict[str, Any] | None:
    for item in workspace.get("source_sets") or []:
        if item.get("set_id") == set_id:
            return dict(item)
    return None


def replace_workspace_set(workspace: dict[str, Any], source_set: dict[str, Any]) -> None:
    set_id = source_set.get("set_id")
    workspace["source_sets"] = [
        source_set if item.get("set_id") == set_id else item for item in workspace.get("source_sets") or []
    ]


def stable_set_id(requested: str, label: str, index: int) -> str:
    if requested:
        return "".join(ch.lower() if ch.isalnum() else "_" for ch in requested)[:48].strip("_") or f"set_{index}"
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())[:44].strip("_")
    return slug or f"set_{index}"


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def invalidate_coverage_syntheses(workspace: dict[str, Any]) -> None:
    """Invalidate question synthesis whenever one of its durable inputs changes."""

    workspace["triage_semantic_revision"] = int(workspace.get("triage_semantic_revision") or 0) + 1
    workspace["coverage_syntheses"] = []
    workspace.pop("source_map_finalization", None)


def merge_deferred(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ref = {str(item.get("source_ref")): dict(item) for item in existing if item.get("source_ref")}
    for item in new_items:
        ref = str(item.get("source_ref") or "")
        if ref:
            by_ref[ref] = {"source_ref": ref, "reason": str(item.get("reason") or "")}
    return list(by_ref.values())


def now() -> str:
    return datetime.utcnow().isoformat()


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def source_set_covered_refs(source_set: dict[str, Any]) -> list[str]:
    refs: list[str] = [str(ref) for ref in source_set.get("assessment_source_refs") or []]
    for item in [*(source_set.get("observations") or []), *(source_set.get("written_actions") or [])]:
        if isinstance(item, dict):
            refs.extend(str(ref) for ref in item.get("source_refs") or [])
    return dedupe(refs)


def unresolved_source_refs(workspace: dict[str, Any]) -> list[str]:
    current = normalize_workspace(workspace)
    resolved = set(str(item) for item in current.get("mapped_sources") or [])
    resolved.update(
        str(item.get("source_ref") or "")
        for item in current.get("deferred_sources") or []
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    )
    for source_set in current.get("source_sets") or []:
        if source_set.get("status") == "mapped":
            resolved.update(source_set_covered_refs(source_set))
        elif source_set.get("status") == "deferred" and str(source_set.get("deferred_reason") or "").strip():
            resolved.update(str(item) for item in source_set.get("source_refs") or [])
    return [str(ref) for ref in current.get("source_order") or [] if str(ref) not in resolved]


def source_set_has_unresolved_refs(workspace: dict[str, Any], source_set: dict[str, Any]) -> bool:
    if source_set.get("status") != "mapped":
        return False
    covered = set(source_set_covered_refs(source_set))
    deferred = {
        str(item.get("source_ref") or "")
        for item in normalize_workspace(workspace).get("deferred_sources") or []
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    }
    return any(str(ref) not in covered and str(ref) not in deferred for ref in source_set.get("source_refs") or [])
