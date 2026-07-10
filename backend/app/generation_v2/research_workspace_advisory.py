from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.research_workspace_contracts import (
    RESEARCH_READS_PER_COMMIT_ADVISORY,
    RESEARCH_READS_PER_COMMIT_WARNING,
)
from app.generation_v2.research_workspace_helpers import parse_dt as _parse_dt
from app.models.generation_v2 import V2ToolTrace

RESEARCH_READ_TOOLS = {
    "search_corpus",
    "find_quotes",
    "read_evidence",
    "inspect_source",
    "read_source_window",
    "read_source_with_model",
    "search_within_source",
    "skim_source",
    "compare_evidence_for_question",
}
RESEARCH_SEMANTIC_COMMIT_TOOLS = {
    "merge_editorial_atoms",
    "record_research_observation",
    "upsert_editorial_atom",
    "finish_research_job",
    "record_research_risk",
    "record_research_gap",
    "finalize_research_memo",
}


async def research_workflow_guard_error(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_job_id: str | None,
) -> dict[str, Any] | None:
    """Update advisory read counters for research jobs without blocking reads."""
    await research_workflow_commit_advisory(
        db,
        generation_run_id=generation_run_id,
        current_job_id=current_job_id,
    )
    return None


async def research_workflow_commit_advisory(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_job_id: str | None,
) -> dict[str, Any] | None:
    if not current_job_id:
        return None
    from app.generation_v2 import research_workspace

    workspace = await research_workspace.load_research_workspace(db, generation_run_id)
    current_job = (workspace or {}).get("current_job") or {}
    started_at = _parse_dt(current_job.get("started_at"))
    if started_at is None:
        return None
    commit_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_editorial_research",
            V2ToolTrace.created_at >= started_at,
            V2ToolTrace.tool_name.in_(list(RESEARCH_SEMANTIC_COMMIT_TOOLS)),
        )
        .order_by(V2ToolTrace.created_at.desc())
        .limit(1)
    )
    last_commit = commit_result.scalar_one_or_none()
    since = last_commit.created_at if last_commit is not None else started_at
    read_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_editorial_research",
            V2ToolTrace.created_at >= since,
            V2ToolTrace.tool_name.in_(list(RESEARCH_READ_TOOLS)),
        )
    )
    read_count = len(list(read_result.scalars()))
    advisory = {
        "reads_since_commit": read_count,
        "warning_threshold": RESEARCH_READS_PER_COMMIT_WARNING,
        "advisory_threshold": RESEARCH_READS_PER_COMMIT_ADVISORY,
        "commit_recommended": read_count >= RESEARCH_READS_PER_COMMIT_WARNING,
        "suggested_next_actions": [
            "record_research_observation",
            "upsert_editorial_atom",
            "record_research_risk",
            "record_research_gap",
        ],
    }
    if workspace is not None and (
        workspace.get("reads_since_commit") != read_count
        or workspace.get("commit_recommended") != advisory["commit_recommended"]
    ):
        workspace["reads_since_commit"] = read_count
        workspace["commit_recommended"] = advisory["commit_recommended"]
        await research_workspace.save_research_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    return advisory if advisory["commit_recommended"] else None


__all__ = [
    "RESEARCH_READ_TOOLS",
    "RESEARCH_SEMANTIC_COMMIT_TOOLS",
    "research_workflow_commit_advisory",
    "research_workflow_guard_error",
]
