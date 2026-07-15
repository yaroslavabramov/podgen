from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.pipeline.research_gate import treatment_gate_research_issues, treatment_repair_gap_id
from app.generation_v2.research_workspace_contracts import SEMANTIC_FIELD_LIMITS
from app.generation_v2.research_workspace_repair_budget import (
    REPAIR_BUDGET_LIMIT,
    TARGETED_REPAIR_BUDGET,
    pending_repair_operation,
    repair_budget,
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
    existing_repair_jobs = _targeted_repair_jobs_if_reserved_or_exhausted(workspace)
    if existing_repair_jobs is not None:
        return existing_repair_jobs
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
        source_refs, evidence_ids, scope_notice = _normalized_audit_issue_scope(workspace, issue)
        resolution_note = issue.suggested_repair or issue.description
        if scope_notice:
            resolution_note = f"{resolution_note} {scope_notice}"
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
                "source_refs": source_refs,
                "evidence_ids": evidence_ids,
                "status": "open",
                "resolution_note": resolution_note,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    if new_gaps:
        workspace["research_gaps"] = [*(workspace.get("research_gaps") or []), *new_gaps]
    audit_repair_feedback: list[dict[str, Any]] = []
    for issue in audit.issues:
        if str(issue.severity) not in {"blocking", "major"}:
            continue
        source_refs, evidence_ids, scope_notice = _normalized_audit_issue_scope(workspace, issue)
        suggested_repair = issue.suggested_repair
        if scope_notice:
            suggested_repair = f"{suggested_repair} {scope_notice}".strip()
        audit_repair_feedback.append(
            {
                "target_step": str(issue.target_step),
                "severity": str(issue.severity),
                "description": issue.description,
                "suggested_repair": suggested_repair,
                "atom_refs": list(issue.atom_refs),
                "source_refs": source_refs,
                "evidence_ids": evidence_ids,
            }
        )
    workspace["audit_repair_feedback"] = audit_repair_feedback
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


def _normalized_audit_issue_scope(
    workspace: dict[str, Any],
    issue: Any,
) -> tuple[list[str], list[str], str]:
    known_source_refs = {
        str(item).upper() for item in workspace.get("source_order") or [] if str(item).strip()
    }
    source_refs: list[str] = []
    invalid_source_count = 0
    for value in issue.source_refs:
        candidate = str(value or "").strip().upper()
        if candidate not in known_source_refs:
            invalid_source_count += 1
            continue
        if candidate not in source_refs:
            source_refs.append(candidate)

    known_evidence_ids = _workspace_current_evidence_ids(workspace)
    evidence_ids: list[str] = []
    invalid_evidence_count = 0
    for value in issue.evidence_ids:
        candidate = str(value or "").strip()
        if candidate not in known_evidence_ids:
            invalid_evidence_count += 1
            continue
        if candidate not in evidence_ids:
            evidence_ids.append(candidate)

    notices: list[str] = []
    if invalid_source_count:
        notices.append(
            f"Backend removed {invalid_source_count} source ref(s) outside the current workspace; "
            "repair must re-establish source scope from the current corpus."
        )
    unresolved_evidence_ref_count = max(
        0,
        len({str(item) for item in issue.evidence_refs if str(item).strip()}) - len(evidence_ids),
    )
    if invalid_evidence_count or unresolved_evidence_ref_count:
        invalid_count = invalid_evidence_count + unresolved_evidence_ref_count
        notices.append(
            f"Backend ignored {invalid_count} unresolved evidence ref(s); "
            "repair must re-establish grounding from current evidence."
        )
    return source_refs, evidence_ids, " ".join(notices)


def _workspace_current_evidence_ids(workspace: dict[str, Any]) -> set[str]:
    evidence_ids = {
        str(item)
        for item in workspace.get("selected_evidence_ids") or []
        if str(item).strip()
    }

    def add_from(item: Any, *keys: str) -> None:
        if not isinstance(item, dict):
            return
        for key in keys:
            evidence_ids.update(str(value) for value in item.get(key) or [] if str(value).strip())

    for job in workspace.get("investigation_jobs") or []:
        add_from(job, "evidence_ids", "starting_evidence_ids")
        if not isinstance(job, dict):
            continue
        for observation in job.get("observations") or []:
            add_from(observation, "evidence_ids")
        for outcome in job.get("question_outcomes") or []:
            add_from(outcome, "evidence_ids")
    for collection in ("atom_drafts", "research_gaps", "risk_register"):
        for item in workspace.get(collection) or []:
            add_from(item, "evidence_ids")
    return evidence_ids


def _targeted_repair_jobs_if_reserved_or_exhausted(workspace: dict[str, Any]) -> list[str] | None:
    """Return the durable repair result, or None while a new repair may be reserved."""
    pending_operation = pending_repair_operation(workspace, TARGETED_REPAIR_BUDGET)
    if pending_operation is not None:
        if pending_operation.get("kind") != "research_jobs":
            return []
        return [
            str(item)
            for item in (pending_operation.get("payload") or {}).get("repair_job_ids") or []
            if str(item).strip()
        ]
    if repair_budget(workspace).get(TARGETED_REPAIR_BUDGET, 0) >= REPAIR_BUDGET_LIMIT:
        return []
    return None


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
    existing_repair_jobs = _targeted_repair_jobs_if_reserved_or_exhausted(workspace)
    if existing_repair_jobs is not None:
        return existing_repair_jobs
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
