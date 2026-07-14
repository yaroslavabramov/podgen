from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.contracts.research import (
    ResearchGapCategory,
    ResearchGapSeverity,
    ResearchGapStatus,
    ResearchGapTreatmentRelevance,
)
from app.generation_v2.research_workspace_helpers import (
    dedupe as _dedupe,
    now as _now,
    stable_id as _stable_id,
)
from app.generation_v2.research_workspace_gap_helpers import (
    dedupe_gaps as _dedupe_gaps,
    repair_atom_kinds_for_gap as _repair_atom_kinds_for_gap,
    treatment_relevance as _treatment_relevance,
)
from app.generation_v2.research_workspace_state import normalize_workspace as _normalize_workspace
from app.generation_v2.research_workspace_repair_budget import (
    TARGETED_REPAIR_BUDGET,
    pending_repair_operation,
    reserve_repair_operation_in_workspace,
)
from app.models.generation_run import GenerationRun


async def enqueue_research_repair_jobs(
    db: AsyncSession,
    *,
    run: GenerationRun,
    max_jobs: int = 4,
) -> list[str]:
    from app.generation_v2 import research_workspace

    current = await research_workspace.load_research_workspace(db, run.id)
    if not current:
        return []
    current = _normalize_workspace(current)
    pending_operation = pending_repair_operation(current, TARGETED_REPAIR_BUDGET)
    if pending_operation is not None:
        if pending_operation.get("kind") != "research_jobs":
            return []
        return [
            str(item)
            for item in (pending_operation.get("payload") or {}).get("repair_job_ids") or []
            if str(item).strip()
        ]
    existing_job_ids = {str(item.get("job_id") or "") for item in current.get("investigation_jobs") or []}
    repair_jobs = _research_repair_jobs(
        current,
        existing_job_ids=existing_job_ids,
        max_jobs=max_jobs,
    )
    if not repair_jobs:
        return []
    current["investigation_jobs"] = [*(current.get("investigation_jobs") or []), *repair_jobs]
    current["status"] = "repair_planned"
    current["current_job"] = None
    current["memo_fields"] = {}
    repair_job_ids = [str(job["job_id"]) for job in repair_jobs]
    if reserve_repair_operation_in_workspace(
        current,
        TARGETED_REPAIR_BUDGET,
        operation_kind="research_jobs",
        payload=_repair_resume_payload(current, repair_job_ids=repair_job_ids),
    ) is None:
        return []
    await research_workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
    return repair_job_ids


def _repair_resume_payload(workspace: dict[str, Any], *, repair_job_ids: list[str]) -> dict[str, Any]:
    return {
        "repair_job_ids": list(repair_job_ids),
        "resume_jobs": deepcopy(workspace.get("investigation_jobs") or []),
        "research_questions": deepcopy(workspace.get("research_questions") or []),
        "research_gaps": deepcopy(workspace.get("research_gaps") or []),
        "audit_repair_feedback": deepcopy(workspace.get("audit_repair_feedback") or []),
    }


def _research_repair_jobs(
    workspace: dict[str, Any],
    *,
    existing_job_ids: set[str],
    max_jobs: int,
) -> list[dict[str, Any]]:
    repair_jobs: list[dict[str, Any]] = []
    for gap in _blocking_research_gaps(workspace)[:max_jobs]:
        question = str(gap.get("question") or "").strip()
        if not question:
            continue
        gap_id = str(gap.get("gap_id") or _stable_id("", question, len(repair_jobs) + 1))
        job_id = _stable_id(f"research_repair_{gap_id}", question, len(existing_job_ids) + len(repair_jobs) + 1)
        if job_id in existing_job_ids:
            continue
        repair_jobs.append(_research_repair_job(gap, gap_id=gap_id, job_id=job_id, question=question))
    return repair_jobs


def _blocking_research_gaps(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        gap
        for gap in _dedupe_gaps(workspace.get("research_gaps") or [])
        if (
            gap.get("status") in {ResearchGapStatus.OPEN.value, ResearchGapStatus.DEFERRED.value}
            and (
                gap.get("severity") == ResearchGapSeverity.BLOCKING.value
                or _treatment_relevance(gap.get("treatment_relevance")) == ResearchGapTreatmentRelevance.CENTRAL.value
            )
        )
    ]


def _research_repair_job(
    gap: dict[str, Any],
    *,
    gap_id: str,
    job_id: str,
    question: str,
) -> dict[str, Any]:
    source_refs = _dedupe([str(item) for item in gap.get("source_refs") or [] if str(item).strip()])
    allow_broad_search = bool(gap.get("category") == ResearchGapCategory.MISSING_SOURCE.value or not source_refs)
    return {
        "job_id": job_id,
        "label": f"Repair research gap {gap_id}",
        "story_job": "research_repair",
        "purpose": (
            "Resolve this blocking research gap from the previous memo gate. "
            "If resolved, persist the source-grounded finding as an atom or observation, then call "
            "record_research_gap with the same gap_id, status='resolved', resolving evidence_ids, "
            "and a resolution_note. If it cannot be resolved, update the same gap as open/deferred "
            "with a concrete reason."
        ),
        "source_refs": source_refs,
        "questions": [question],
        "required_atom_kinds": _repair_atom_kinds_for_gap(gap),
        "priority": "repair",
        "allow_broad_search": allow_broad_search,
        "status": "planned",
        "observations": [],
        "atom_ids": [],
        "evidence_ids": [],
        "summary": "",
        "deferred_reason": "",
        "question_outcomes": [],
        "attempt": 0,
        "repair_for_gap_id": gap_id,
        "repair_gap": {
            "gap_id": gap_id,
            "question": question,
            "category": str(gap.get("category") or ""),
            "severity": str(gap.get("severity") or ResearchGapSeverity.BLOCKING.value),
            "treatment_relevance": _treatment_relevance(gap.get("treatment_relevance")),
            "source_refs": source_refs,
            "evidence_ids": _dedupe([str(item) for item in gap.get("evidence_ids") or [] if str(item).strip()]),
            "status": str(gap.get("status") or "open"),
        },
        "updated_at": _now(),
    }


__all__ = ["enqueue_research_repair_jobs"]
