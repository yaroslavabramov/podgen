from __future__ import annotations

import time
from copy import deepcopy
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.pipeline.research_gate import treatment_gate_research_issues, treatment_repair_gap_id
from app.generation_v2.research_workspace_contracts import SEMANTIC_FIELD_LIMITS
from app.generation_v2.research_workspace_repair_budget import (
    TARGETED_REPAIR_BUDGET,
    pending_repair_operation,
    reserve_repair_operation_in_workspace,
)
from app.generation_v2.schemas import EpisodeCritique, ResearchAudit
from app.models.generation_run import GenerationRun

WorkspaceLoader = Callable[[AsyncSession, Any], Awaitable[dict[str, Any] | None]]
WorkspaceSaver = Callable[..., Awaitable[Any]]
RepairJobEnqueuer = Callable[..., Awaitable[list[str]]]


async def enqueue_audit_research_repair_jobs(
    db: AsyncSession,
    *,
    run: GenerationRun,
    audit: ResearchAudit,
) -> list[str]:
    from app.generation_v2 import research_workspace

    workspace = await research_workspace.load_research_workspace(db, run.id)
    if not workspace:
        return []
    existing_gap_ids = {
        str(item.get("gap_id") or "")
        for item in workspace.get("research_gaps") or []
        if isinstance(item, dict)
    }
    new_gaps: list[dict[str, Any]] = []
    for idx, issue in enumerate(audit.issues, start=1):
        if str(issue.target_step) != "editorial_research" or str(issue.severity) not in {"blocking", "major"}:
            continue
        _validate_repair_semantic_field("question", issue.description)
        resolution_note = issue.suggested_repair or issue.description
        _validate_repair_semantic_field("resolution_note", resolution_note)
        gap_id = treatment_repair_gap_id(f"audit_{issue.description}", idx)
        if gap_id in existing_gap_ids:
            continue
        new_gaps.append(
            {
                "gap_id": gap_id,
                "question": issue.description,
                "category": "weak_corroboration",
                "severity": "blocking",
                "treatment_relevance": "central",
                "source_refs": list(issue.source_refs),
                "evidence_ids": list(issue.evidence_ids),
                "status": "open",
                "resolution_note": resolution_note,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    if new_gaps:
        workspace["research_gaps"] = [*(workspace.get("research_gaps") or []), *new_gaps]
    workspace["audit_repair_feedback"] = [
        {
            "target_step": str(issue.target_step),
            "severity": str(issue.severity),
            "description": issue.description,
            "suggested_repair": issue.suggested_repair,
            "atom_refs": list(issue.atom_refs),
            "source_refs": list(issue.source_refs),
            "evidence_ids": list(issue.evidence_ids),
        }
        for issue in audit.issues
        if str(issue.severity) in {"blocking", "major"}
    ]
    workspace["status"] = "audit_research_repair_needed"
    workspace["current_job"] = None
    workspace["memo_fields"] = {}
    await research_workspace.save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
    return await research_workspace.enqueue_research_repair_jobs(db, run=run)


async def prepare_research_finalizer_repair(
    db: AsyncSession,
    *,
    run: GenerationRun,
    audit: ResearchAudit,
) -> bool:
    from app.generation_v2 import research_workspace

    workspace = await research_workspace.load_research_workspace(db, run.id)
    if not workspace:
        return False
    pending_operation = pending_repair_operation(workspace, TARGETED_REPAIR_BUDGET)
    if pending_operation is not None:
        return pending_operation.get("kind") == "research_finalizer"
    issues = [
        issue
        for issue in audit.issues
        if str(issue.target_step) == "research_finalizer" and str(issue.severity) in {"blocking", "major"}
    ]
    if not issues:
        return False
    feedback = [
        {
            "target_step": "research_finalizer",
            "severity": str(issue.severity),
            "description": issue.description,
            "suggested_repair": issue.suggested_repair,
            "atom_refs": list(issue.atom_refs),
        }
        for issue in issues
    ]
    workspace["audit_repair_feedback"] = feedback
    workspace["memo_fields"] = {}
    workspace["status"] = "finalizer_repair_needed"
    if reserve_repair_operation_in_workspace(
        workspace,
        TARGETED_REPAIR_BUDGET,
        operation_kind="research_finalizer",
        payload={
            "resume_jobs": deepcopy(workspace.get("investigation_jobs") or []),
            "research_questions": deepcopy(workspace.get("research_questions") or []),
            "research_gaps": deepcopy(workspace.get("research_gaps") or []),
            "audit_repair_feedback": deepcopy(feedback),
        },
    ) is None:
        return False
    await research_workspace.save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
    return True


def _validate_repair_semantic_field(field: str, value: str) -> None:
    limit = SEMANTIC_FIELD_LIMITS[field]
    if len(value) > limit:
        raise ValueError(
            f"semantic_field_too_long: field={field}, max_length={limit}, actual_length={len(value)}"
        )


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
