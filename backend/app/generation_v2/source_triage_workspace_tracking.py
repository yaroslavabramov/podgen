from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.runtime_tool_outcomes import TOOL_OUTCOME_CONTROL, successful_read_output
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
from app.models.generation_v2 import V2ToolTrace


SOURCE_SET_COMMIT_CONTROL_CODE = "source_set_state_commit_required"
SOURCE_SET_COMMIT_ACTIONS = [
    "save_source_analysis",
    "resolve_triage_question",
    "finish_triage_source_set",
]


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
    "completed_read_control",
    "mark_workspace_write_action",
    "triage_workflow_guard_error",
    "triage_workflow_read_control",
]
