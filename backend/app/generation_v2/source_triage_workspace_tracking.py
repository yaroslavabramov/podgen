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
    replace_workspace_set as _replace_workspace_set,
    workspace_set as _workspace_set,
)
from app.generation_v2.runtime_tool_outcomes import TOOL_OUTCOME_CONTROL, successful_read_output
from app.models.generation_v2 import V2ToolTrace


SOURCE_SET_COMMIT_CONTROL_CODE = "source_set_state_commit_required"
SOURCE_SET_COMMIT_ACTIONS = [
    "save_source_analysis",
    "upsert_coverage_outcome",
    "finish_triage_source_set",
]
SOURCE_SET_EXACT_READ_CONTROL_CODE = "source_analysis_pending_after_exact_read"


def _resolved_source_refs(workspace: dict[str, Any], *, set_id: str) -> set[str]:
    return {
        str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "") == "resolved"
        and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
    }


def pending_exact_read_source_refs(
    workspace: dict[str, Any],
    *,
    source_set: dict[str, Any],
) -> list[str]:
    """Return exact-read sources that still lack a durable source analysis."""

    set_id = str(source_set.get("set_id") or "")
    allowed_refs = {str(item) for item in source_set.get("source_refs") or []}
    resolved_refs = _resolved_source_refs(workspace, set_id=set_id)
    return _dedupe(
        [
            str(item.get("source_ref") or "")
            for item in source_set.get("pending_exact_reads") or []
            if isinstance(item, dict)
            and str(item.get("source_ref") or "") in allowed_refs
            and str(item.get("source_ref") or "") not in resolved_refs
        ]
    )


def clear_pending_exact_read(
    source_set: dict[str, Any],
    *,
    source_ref: str,
) -> None:
    """Clear the operational read marker in the same write as source analysis."""

    source_set["pending_exact_reads"] = [
        item
        for item in source_set.get("pending_exact_reads") or []
        if not isinstance(item, dict) or str(item.get("source_ref") or "") != source_ref
    ]


async def record_triage_exact_read(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_set_id: str | None,
    source_ref: str,
    evidence_ids: list[str],
) -> dict[str, Any] | None:
    """Persist a successful substantive exact read as operational unfinished work."""

    workspace = await load_source_triage_workspace(db, generation_run_id)
    if not workspace or not current_set_id or not source_ref or not evidence_ids:
        return None
    source_set = _workspace_set(workspace, current_set_id)
    if source_set is None or source_ref not in {str(item) for item in source_set.get("source_refs") or []}:
        return None
    if source_ref in _resolved_source_refs(workspace, set_id=current_set_id):
        clear_pending_exact_read(source_set, source_ref=source_ref)
    else:
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
            "This source still needs a completed analysis. Continue reading, compare sources, or save it when the "
            "semantic analysis is ready; other sources remain readable."
        ),
    }


async def mark_workspace_write_action(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    tool_name: str,
    source_refs: list[str],
    evidence_ids: list[str],
) -> None:
    workspace = await load_source_triage_workspace(db, generation_run_id)
    if not workspace:
        return
    current_set = workspace.get("current_set") or {}
    set_id = str(current_set.get("set_id") or "")
    source_set = _workspace_set(workspace, set_id) if set_id else None
    if source_set is None:
        return
    action = {
        "tool_name": tool_name,
        "source_refs": _dedupe(source_refs),
        "evidence_ids": _dedupe(evidence_ids),
        "created_at": _now(),
    }
    source_set.setdefault("written_actions", []).append(action)
    source_set["evidence_ids"] = _dedupe([*(source_set.get("evidence_ids") or []), *evidence_ids])
    source_set["updated_at"] = _now()
    _replace_workspace_set(workspace, source_set)
    workspace["mapped_sources"] = _dedupe([*(workspace.get("mapped_sources") or []), *source_refs])
    await save_source_triage_workspace(db, generation_run_id=generation_run_id, workspace=workspace)


async def triage_workflow_guard_error(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_set_id: str | None,
) -> dict[str, Any] | None:
    del db, generation_run_id, current_set_id
    return None


async def triage_workflow_read_control(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_set_id: str | None,
) -> dict[str, Any] | None:
    del db, generation_run_id, current_set_id
    # The agent, not a fixed read counter, decides when a source is understood
    # well enough to persist. Long sources may legitimately need many reads.
    return None


def completed_read_control(control: dict[str, Any]) -> dict[str, Any]:
    read_count = int(control.get("read_calls_since_commit") or 0) + 1
    return {
        "control_code": SOURCE_SET_COMMIT_CONTROL_CODE,
        "set_id": str(control.get("set_id") or ""),
        "read_calls_since_commit": read_count,
        "remaining_reads": None,
        "must_persist_before_more_reads": False,
        "message": "Continue reading or persist a completed source analysis.",
        "expected_next_actions": list(control.get("expected_next_actions") or SOURCE_SET_COMMIT_ACTIONS),
    }


def blocked_read_control(control: dict[str, Any]) -> dict[str, Any]:
    return {
        "outcome_kind": TOOL_OUTCOME_CONTROL,
        "control_code": SOURCE_SET_COMMIT_CONTROL_CODE,
        "read_performed": False,
        "set_id": str(control.get("set_id") or ""),
        "read_calls_since_commit": int(control.get("read_calls_since_commit") or 0),
        "remaining_reads": 0,
        "must_persist_before_more_reads": True,
        "message": (
            "No read was performed. Persist a source assessment, coverage outcome, observation, SourceMap write, "
            "or finish the set, then continue reading."
        ),
        "expected_next_actions": list(control.get("expected_next_actions") or SOURCE_SET_COMMIT_ACTIONS),
    }


def _successful_read_trace(trace: V2ToolTrace) -> bool:
    output = trace.output_json if isinstance(trace.output_json, dict) else {}
    return successful_read_output(output)


def _successful_commit_trace(trace: V2ToolTrace) -> bool:
    output = trace.output_json if isinstance(trace.output_json, dict) else {}
    return output.get("accepted") is True and not output.get("error") and not output.get(
        "_runtime_error"
    )


__all__ = [
    "blocked_read_control",
    "clear_pending_exact_read",
    "completed_read_control",
    "mark_workspace_write_action",
    "pending_exact_read_source_refs",
    "record_triage_exact_read",
    "triage_workflow_guard_error",
    "triage_workflow_read_control",
]
