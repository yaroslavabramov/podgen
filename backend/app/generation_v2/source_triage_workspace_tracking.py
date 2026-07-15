from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_READ_TOOLS,
    TRIAGE_READS_PER_COMMIT,
    TRIAGE_SEMANTIC_COMMIT_TOOLS,
)
from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_state import (
    dedupe as _dedupe,
    invalidate_coverage_syntheses as _invalidate_coverage_syntheses,
    now as _now,
    parse_dt as _parse_dt,
    replace_workspace_set as _replace_workspace_set,
    workspace_set as _workspace_set,
)
from app.models.generation_v2 import V2ToolTrace


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
    _invalidate_coverage_syntheses(workspace)
    current_set = workspace.get("current_set") or {}
    set_id = str(current_set.get("set_id") or "")
    source_set = _workspace_set(workspace, set_id) if set_id else None
    if source_set is None:
        await save_source_triage_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
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
    if not current_set_id:
        return None
    workspace = await load_source_triage_workspace(db, generation_run_id)
    revision = int((workspace or {}).get("corpus_revision") or 1)
    attempt = int((workspace or {}).get("attempt") or 1)
    current_set = (workspace or {}).get("current_set") or {}
    started_at = _parse_dt(current_set.get("started_at"))
    if started_at is None:
        return None
    commit_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.corpus_revision == revision,
            V2ToolTrace.attempt == attempt,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.created_at >= started_at,
            V2ToolTrace.tool_name.in_(list(TRIAGE_SEMANTIC_COMMIT_TOOLS)),
        )
        .order_by(V2ToolTrace.created_at.desc())
        .limit(1)
    )
    last_commit = commit_result.scalar_one_or_none()
    since = last_commit.created_at if last_commit is not None else started_at
    read_result = await db.execute(
        select(V2ToolTrace).where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.corpus_revision == revision,
            V2ToolTrace.attempt == attempt,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.created_at >= since,
            V2ToolTrace.tool_name.in_(list(TRIAGE_READ_TOOLS)),
        )
    )
    read_count = len(list(read_result.scalars()))
    if read_count < TRIAGE_READS_PER_COMMIT:
        return None
    return {
        "accepted": False,
        "error": "source_set_requires_state_commit",
        "message": "You have read/searched enough in this source set. Persist an assessment, coverage outcome, observation, SourceMap write, or finish the set before more reading/searching.",
        "set_id": current_set_id,
        "read_calls_since_commit": read_count,
        "expected_next_actions": [
            "record_triage_observation",
            "upsert_source_assessment",
            "upsert_coverage_outcome",
            "upsert_source_card",
            "record_source_cluster",
            "record_source_relationship",
            "record_gap",
            "resolve_gap",
            "finish_triage_source_set",
        ],
    }


__all__ = ["mark_workspace_write_action", "triage_workflow_guard_error"]
