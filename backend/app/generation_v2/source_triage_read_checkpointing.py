from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_state import (
    dedupe as _dedupe,
    now as _now,
    parse_dt as _parse_dt,
    replace_workspace_set as _replace_workspace_set,
    source_read_receipt_is_delivered,
    uncheckpointed_source_read_replays,
    workspace_set as _workspace_set,
)


SOURCE_SET_EXACT_READ_CONTROL_CODE = "source_analysis_pending_after_exact_read"
SOURCE_SET_COMPARISON_CONTROL_CODE = "source_analysis_pending_after_comparison"


async def source_checkpoint_transition_guard(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_set_id: str | None,
    target_source_refs: list[str],
    exact_read: bool,
) -> dict[str, Any] | None:
    """Prevent source switches while newly read meaning exists only in context."""

    del exact_read
    if not current_set_id or not target_source_refs:
        return None
    workspace = await load_source_triage_workspace(db, generation_run_id)
    if not workspace:
        return None
    source_set = _workspace_set(workspace, current_set_id)
    if source_set is None:
        return None
    target_refs = {str(value) for value in target_source_refs if str(value).strip()}
    if not target_refs:
        return None

    blockers = {
        str(item.get("source_ref") or "")
        for item in uncheckpointed_source_read_replays(workspace, set_id=current_set_id)
        if str(item.get("source_ref") or "")
    }
    blockers.update(_uncheckpointed_exact_read_refs(workspace, source_set=source_set))

    if len(target_refs) > 1:
        # An old note is not enough after a later read. Every source entering a
        # comparison must have durable memory current through its latest read.
        missing_checkpoint_refs = sorted(
            (
                target_refs
                - _checkpointed_source_refs(workspace, set_id=current_set_id)
            )
            | (target_refs & blockers)
        )
        if missing_checkpoint_refs:
            return {
                "accepted": False,
                "found": False,
                "error": "checkpoint_sources_before_comparison",
                "message": "No comparison content was read. Each source needs durable reading memory first.",
                "checkpoint_required_source_refs": missing_checkpoint_refs,
                "requested_source_refs": sorted(target_refs),
                "allowed_next_actions": ["read_source", "save_reading_note", "save_source_analysis"],
                "next_action": (
                    "Read and checkpoint each listed source individually, then retry the comparison."
                ),
            }

    switch_blockers = sorted(blockers - target_refs)
    if not switch_blockers:
        return None
    return {
        "accepted": False,
        "found": False,
        "error": "checkpoint_current_source_before_switch",
        "message": (
            "No source content was read. Save a semantic reading note or completed source analysis for the "
            "current source before opening a different source."
        ),
        "checkpoint_required_source_refs": switch_blockers,
        "requested_source_refs": sorted(target_refs),
        "allowed_next_actions": ["save_reading_note", "save_source_analysis"],
        "next_action": (
            "Call save_reading_note for the listed source using its delivered receipt_refs, or save its complete "
            "source analysis; then retry this read."
        ),
    }


def _uncheckpointed_exact_read_refs(
    workspace: dict[str, Any],
    *,
    source_set: dict[str, Any],
) -> set[str]:
    set_id = str(source_set.get("set_id") or "")
    latest_analysis_times: dict[str, Any] = {}
    for assessment in workspace.get("source_assessments") or []:
        if not isinstance(assessment, dict) or str(assessment.get("set_id") or "") != set_id:
            continue
        source_ref = str(assessment.get("source_ref") or "")
        updated_at = _parse_dt(assessment.get("updated_at"))
        if not source_ref or updated_at is None:
            continue
        previous = latest_analysis_times.get(source_ref)
        if previous is None or updated_at > previous:
            latest_analysis_times[source_ref] = updated_at
    latest_note_times: dict[str, Any] = {}
    for note in workspace.get("reading_notes") or []:
        if not isinstance(note, dict) or str(note.get("set_id") or "") != set_id:
            continue
        source_ref = str(note.get("source_ref") or "")
        updated_at = _parse_dt(note.get("updated_at"))
        if not source_ref or updated_at is None:
            continue
        previous = latest_note_times.get(source_ref)
        if previous is None or updated_at > previous:
            latest_note_times[source_ref] = updated_at

    pending: set[str] = set()
    for item in source_set.get("pending_exact_reads") or []:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        if not source_ref:
            continue
        exact_read_at = _parse_dt(item.get("updated_at"))
        note_at = latest_note_times.get(source_ref)
        analysis_at = latest_analysis_times.get(source_ref)
        checkpoint_at = max(
            [value for value in (note_at, analysis_at) if value is not None],
            default=None,
        )
        if exact_read_at is None or checkpoint_at is None or checkpoint_at < exact_read_at:
            pending.add(source_ref)
    return pending


def _resolved_source_refs(workspace: dict[str, Any], *, set_id: str) -> set[str]:
    return {
        str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "") == "resolved"
        and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
    }


def _checkpointed_source_refs(workspace: dict[str, Any], *, set_id: str) -> set[str]:
    refs = _resolved_source_refs(workspace, set_id=set_id)
    refs.update(
        str(item.get("source_ref") or "")
        for item in workspace.get("reading_notes") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "")
        and str(item.get("what_was_read") or "").strip()
        and str(item.get("what_was_established") or "").strip()
    )
    return refs


def pending_exact_read_source_refs(
    workspace: dict[str, Any],
    *,
    source_set: dict[str, Any],
) -> list[str]:
    allowed_refs = {str(item) for item in source_set.get("source_refs") or []}
    return _dedupe(
        [
            source_ref
            for source_ref in _uncheckpointed_exact_read_refs(workspace, source_set=source_set)
            if source_ref in allowed_refs
        ]
    )


def clear_pending_exact_read(source_set: dict[str, Any], *, source_ref: str) -> None:
    source_set["pending_exact_reads"] = [
        item
        for item in source_set.get("pending_exact_reads") or []
        if not isinstance(item, dict) or str(item.get("source_ref") or "") != source_ref
    ]


def pending_comparison_source_refs(source_set: dict[str, Any]) -> list[str]:
    allowed_refs = {str(item) for item in source_set.get("source_refs") or []}
    return _dedupe(
        [
            str(item.get("source_ref") or "")
            for item in source_set.get("pending_comparison_reads") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "") in allowed_refs
        ]
    )


def clear_pending_comparison_read(source_set: dict[str, Any], *, source_ref: str) -> None:
    source_set["pending_comparison_reads"] = [
        item
        for item in source_set.get("pending_comparison_reads") or []
        if not isinstance(item, dict) or str(item.get("source_ref") or "") != source_ref
    ]


def source_refs_with_reads_newer_than_analysis(
    workspace: dict[str, Any],
    *,
    set_id: str,
) -> list[str]:
    assessments = {
        str(item.get("source_ref") or ""): item
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "")
    }
    receipts_by_source: dict[str, list[dict[str, Any]]] = {}
    for item in workspace.get("source_read_receipts") or []:
        if (
            isinstance(item, dict)
            and str(item.get("set_id") or "") == set_id
            and str(item.get("source_ref") or "")
            and source_read_receipt_is_delivered(item)
        ):
            receipts_by_source.setdefault(str(item.get("source_ref") or ""), []).append(item)

    stale_refs: list[str] = []
    for source_ref, receipts in receipts_by_source.items():
        assessment = assessments.get(source_ref)
        if assessment is None:
            stale_refs.append(source_ref)
            continue
        reading_basis = assessment.get("reading_basis")
        if isinstance(reading_basis, dict) and "receipt_refs" in reading_basis:
            cited_refs = {str(value) for value in reading_basis.get("receipt_refs") or []}
            delivered_refs = {str(item.get("receipt_ref") or "") for item in receipts}
            if any(receipt_ref and receipt_ref not in cited_refs for receipt_ref in delivered_refs):
                stale_refs.append(source_ref)
            continue
        analysis_at = _parse_dt(assessment.get("updated_at"))
        latest_read_at = max(
            [
                timestamp
                for item in receipts
                if (
                    timestamp := _parse_dt(
                        item.get("delivered_at") or item.get("created_at") or item.get("updated_at")
                    )
                )
                is not None
            ],
            default=None,
        )
        if analysis_at is not None and latest_read_at is not None and latest_read_at > analysis_at:
            stale_refs.append(source_ref)
    return sorted(set(stale_refs))


async def record_triage_comparison_read(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_set_id: str | None,
    evidence_ids_by_source_ref: dict[str, list[str]],
) -> dict[str, Any] | None:
    if not current_set_id or not evidence_ids_by_source_ref:
        return None
    workspace = await load_source_triage_workspace(db, generation_run_id)
    if not workspace:
        return None
    source_set = _workspace_set(workspace, current_set_id)
    if source_set is None:
        return None
    allowed_refs = {str(item) for item in source_set.get("source_refs") or []}
    now = _now()
    for source_ref, evidence_ids in evidence_ids_by_source_ref.items():
        if source_ref not in allowed_refs or not evidence_ids:
            continue
        clear_pending_comparison_read(source_set, source_ref=source_ref)
        source_set.setdefault("pending_comparison_reads", []).append(
            {
                "source_ref": source_ref,
                "evidence_ids": _dedupe([str(value) for value in evidence_ids]),
                "updated_at": now,
            }
        )
    pending_refs = pending_comparison_source_refs(source_set)
    if not pending_refs:
        return None
    source_set["updated_at"] = now
    _replace_workspace_set(workspace, source_set)
    await save_source_triage_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    return {
        "control_code": SOURCE_SET_COMPARISON_CONTROL_CODE,
        "set_id": current_set_id,
        "pending_source_refs": pending_refs,
        "analysis_pending": True,
        "message": (
            "The comparison exposed evidence that must be reconciled into each listed source analysis before "
            "the set can finish."
        ),
    }


async def record_triage_exact_read(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_set_id: str | None,
    source_ref: str,
    evidence_ids: list[str],
) -> dict[str, Any] | None:
    workspace = await load_source_triage_workspace(db, generation_run_id)
    if not workspace or not current_set_id or not source_ref or not evidence_ids:
        return None
    source_set = _workspace_set(workspace, current_set_id)
    if source_set is None or source_ref not in {str(item) for item in source_set.get("source_refs") or []}:
        return None
    existing = next(
        (
            item
            for item in source_set.get("pending_exact_reads") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref
        ),
        None,
    )
    entry = {
        "source_ref": source_ref,
        "evidence_ids": _dedupe(
            [
                *(
                    [str(item) for item in existing.get("evidence_ids") or []]
                    if isinstance(existing, dict)
                    else []
                ),
                *[str(item) for item in evidence_ids if str(item)],
            ]
        ),
        "updated_at": _now(),
    }
    clear_pending_exact_read(source_set, source_ref=source_ref)
    source_set.setdefault("pending_exact_reads", []).append(entry)
    source_set["updated_at"] = _now()
    _replace_workspace_set(workspace, source_set)
    await save_source_triage_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    pending_refs = pending_exact_read_source_refs(workspace, source_set=source_set)
    return {
        "control_code": SOURCE_SET_EXACT_READ_CONTROL_CODE,
        "set_id": current_set_id,
        "pending_source_refs": pending_refs,
        "analysis_pending": bool(pending_refs),
        "message": (
            "This source now has exact material newer than its last checkpoint. Continue reading or save a reading "
            "note/source analysis before switching sources."
        ),
    }


__all__ = [
    "clear_pending_comparison_read",
    "clear_pending_exact_read",
    "pending_comparison_source_refs",
    "pending_exact_read_source_refs",
    "record_triage_comparison_read",
    "record_triage_exact_read",
    "source_checkpoint_transition_guard",
    "source_refs_with_reads_newer_than_analysis",
]
