from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.research_workspace_helpers import dedupe as _dedupe
from app.generation_v2.research_workspace_helpers import now as _now
from app.models.generation_run import GenerationRun


def record_research_risk_handler(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    evidence_toolset: Any | None,
    current_job_id: str | None,
):
    async def record_research_risk_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace._workspace_job(current, job_id) if job_id else None
        risk = str(args.get("risk") or "").strip()
        if not risk:
            return {"accepted": False, "error": "risk is required"}
        field_error = _workspace._semantic_field_error({"risk": risk}, ["risk"])
        if field_error:
            return field_error
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_refs = [ref for ref in source_refs if ref not in set(current.get("source_order") or [])]
        if invalid_refs:
            return {"accepted": False, "error": "risk source_refs must exist in workspace", "invalid_source_refs": invalid_refs}
        if job is not None:
            invalid_refs = _workspace._refs_outside_job(job, source_refs)
            if invalid_refs:
                return {"accepted": False, "error": "risk source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = (
            await _workspace._invalid_job_evidence_ids(evidence_toolset, job, evidence_ids, source_refs=source_refs)
            if job is not None
            else await _workspace._invalid_workspace_evidence_ids(evidence_toolset, evidence_ids)
        )
        if invalid_evidence:
            error = (
                "risk evidence_ids must belong to selected source_refs and current job scope"
                if job is not None
                else "risk evidence_ids must exist in this run"
            )
            return {"accepted": False, "error": error, "invalid_evidence_ids": invalid_evidence}
        atom_refs, invalid_atom_refs = _workspace._resolve_workspace_atom_refs(current, args.get("atom_refs") or [])
        if invalid_atom_refs:
            return {"accepted": False, "error": "risk atom_refs must exist in workspace", "invalid_atom_refs": invalid_atom_refs}
        current.setdefault("risk_register", []).append(
            {
                "risk": risk,
                "severity": str(args.get("severity") or "medium").strip() or "medium",
                "source_refs": source_refs,
                "evidence_ids": evidence_ids,
                "atom_refs": atom_refs,
                "created_at": _now(),
            }
        )
        _workspace._mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "risk_count": len(current.get("risk_register") or [])}

    return record_research_risk_tool
