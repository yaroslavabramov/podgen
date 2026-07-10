from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.pipeline.research_gate import treatment_gate_research_issues, treatment_repair_gap_id
from app.generation_v2.schemas import EpisodeCritique
from app.models.generation_run import GenerationRun

WorkspaceLoader = Callable[[AsyncSession, Any], Awaitable[dict[str, Any] | None]]
WorkspaceSaver = Callable[..., Awaitable[Any]]
RepairJobEnqueuer = Callable[..., Awaitable[list[str]]]


async def enqueue_treatment_research_repair_jobs(
    db: AsyncSession,
    *,
    run: GenerationRun,
    treatment_gate: EpisodeCritique,
    load_workspace: WorkspaceLoader,
    save_workspace: WorkspaceSaver,
    enqueue_repair_jobs: RepairJobEnqueuer,
) -> list[str]:
    issues = treatment_gate_research_issues(treatment_gate)
    if not issues:
        return []
    workspace = await load_workspace(db, run.id)
    if not workspace:
        return []
    existing_gap_ids = {
        str(item.get("gap_id") or "")
        for item in workspace.get("research_gaps") or []
        if isinstance(item, dict)
    }
    new_gaps: list[dict[str, Any]] = []
    for idx, issue in enumerate(issues, start=1):
        gap_id = treatment_repair_gap_id(issue, idx)
        if gap_id in existing_gap_ids:
            continue
        new_gaps.append(
            {
                "gap_id": gap_id,
                "question": issue,
                "category": "weak_corroboration",
                "severity": "blocking",
                "treatment_relevance": "central",
                "source_refs": [],
                "evidence_ids": [],
                "status": "open",
                "resolution_note": "Created by treatment gate; resolve or bound before treatment can anchor this material.",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    if not new_gaps:
        return []
    workspace["research_gaps"] = [*(workspace.get("research_gaps") or []), *new_gaps]
    workspace["status"] = "treatment_repair_needed"
    workspace["current_job"] = None
    workspace["memo_fields"] = {}
    await save_workspace(db, generation_run_id=run.id, workspace=workspace)
    return await enqueue_repair_jobs(db, run=run)
