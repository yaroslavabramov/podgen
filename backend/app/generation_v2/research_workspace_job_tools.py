from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.persistence_contracts import semantic_list_error
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_PENDING_QUESTION_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_helpers import dedupe as _dedupe
from app.generation_v2.research_workspace_helpers import now as _now
from app.models.generation_run import GenerationRun


def record_research_observation_handler(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    evidence_toolset: Any | None,
    current_job_id: str | None,
):
    async def record_research_observation_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace._workspace_job(current, job_id)
        if job is None:
            return {"accepted": False, "error": "Unknown research job", "job_id": job_id}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_workspace_refs = _workspace._invalid_workspace_refs(current, source_refs)
        if invalid_workspace_refs:
            return {
                "accepted": False,
                "error": "observation source_refs must exist in the research workspace",
                "invalid_source_refs": invalid_workspace_refs,
            }
        invalid_refs = _workspace._refs_outside_job(job, source_refs)
        if invalid_refs:
            return {"accepted": False, "error": "observation source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        note = str(args.get("note") or "").strip()
        if not note:
            return {"accepted": False, "error": "note is required"}
        field_error = _workspace._semantic_field_error({"note": note}, ["note"])
        if field_error:
            return field_error
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = await _workspace._invalid_job_evidence_ids(evidence_toolset, job, evidence_ids)
        if invalid_evidence:
            return {
                "accepted": False,
                "error": "observation evidence_ids must be observed evidence from this job scope",
                "invalid_evidence_ids": invalid_evidence,
            }
        observation = {
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "note": note,
            "pending_questions": [str(item).strip() for item in args.get("pending_questions") or [] if str(item).strip()],
            "created_at": _now(),
        }
        list_error = semantic_list_error(
            field="pending_questions",
            values=observation["pending_questions"],
            max_items=RESEARCH_PENDING_QUESTION_LIMIT,
            item_limit=SEMANTIC_FIELD_LIMITS["pending_question"],
        )
        if list_error:
            return list_error
        job.setdefault("observations", []).append(observation)
        job["evidence_ids"] = _dedupe([*(job.get("evidence_ids") or []), *evidence_ids])
        job["updated_at"] = _now()
        _workspace._replace_workspace_job(current, job)
        _workspace._mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "observation_count": len(job.get("observations") or []), "evidence_count": len(evidence_ids)}

    return record_research_observation_tool


def finish_research_job_handler(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    current_job_id: str | None,
):
    async def finish_research_job_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace._workspace_job(current, job_id)
        if job is None:
            return {"accepted": False, "error": "Unknown research job", "job_id": job_id}
        status = str(args.get("status") or "").strip()
        if status not in {"mapped", "deferred"}:
            return {"accepted": False, "error": "status must be mapped or deferred"}
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return {"accepted": False, "error": "summary is required"}
        if status == "mapped" and not (job.get("observations") or job.get("atom_ids") or job.get("evidence_ids")):
            return {"accepted": False, "error": "mapped research job requires at least one observation, atom draft, or evidence_id"}
        if status == "deferred" and not str(args.get("deferred_reason") or "").strip():
            return {"accepted": False, "error": "deferred_reason is required for deferred status"}
        deferred_reason = str(args.get("deferred_reason") or "").strip()
        field_error = _workspace._semantic_field_error(
            {"job_summary": summary, "deferred_reason": deferred_reason},
            ["job_summary", "deferred_reason"],
        )
        if field_error:
            return field_error
        job["status"] = status
        job["summary"] = summary
        job["deferred_reason"] = deferred_reason
        job["updated_at"] = _now()
        _workspace._replace_workspace_job(current, job)
        processed = [item for item in current.get("processed_jobs") or [] if item.get("job_id") != job_id]
        processed.append({"job_id": job_id, "status": status, "summary": job["summary"], "updated_at": _now()})
        current["processed_jobs"] = processed
        current["current_job"] = None
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "status": status, "processed_job_count": len(processed)}

    return finish_research_job_tool
