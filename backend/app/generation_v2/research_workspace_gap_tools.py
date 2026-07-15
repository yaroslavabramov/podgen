from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.contracts.research import (
    ResearchGapSeverity,
    ResearchGapStatus,
    ResearchGapTreatmentRelevance,
)
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_GAP_CATEGORIES,
    RESEARCH_GAP_SEVERITIES,
    RESEARCH_GAP_STATUSES,
    RESEARCH_GAP_TREATMENT_RELEVANCE,
)
from app.generation_v2.research_workspace_helpers import (
    dedupe as _dedupe,
    gap_key as _gap_key,
    merge_research_gap as _merge_research_gap,
    now as _now,
    stable_id as _stable_id,
)
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_SELECTED
from app.models.generation_run import GenerationRun


def record_research_gap_handler(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    evidence_toolset: Any | None,
    current_job_id: str | None,
):
    async def record_research_gap_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace._workspace_job(current, job_id) if job_id else None
        question = str(args.get("question") or "").strip()
        if not question:
            return {"accepted": False, "error": "gap question is required"}
        field_error = _workspace._semantic_field_error({"question": question}, ["question"])
        if field_error:
            return field_error
        category = str(args.get("category") or "").strip()
        if category not in RESEARCH_GAP_CATEGORIES:
            return {"accepted": False, "error": "gap category is invalid", "allowed_categories": sorted(RESEARCH_GAP_CATEGORIES)}
        severity = str(args.get("severity") or "").strip()
        if severity not in RESEARCH_GAP_SEVERITIES:
            return {"accepted": False, "error": "gap severity is invalid", "allowed_severities": sorted(RESEARCH_GAP_SEVERITIES)}
        treatment_relevance = str(args.get("treatment_relevance") or ResearchGapTreatmentRelevance.SUPPORTING.value).strip()
        if treatment_relevance not in RESEARCH_GAP_TREATMENT_RELEVANCE:
            return {
                "accepted": False,
                "error": "gap treatment_relevance is invalid",
                "allowed_treatment_relevance": sorted(RESEARCH_GAP_TREATMENT_RELEVANCE),
            }
        status = str(args.get("status") or ResearchGapStatus.OPEN.value).strip() or ResearchGapStatus.OPEN.value
        if status not in RESEARCH_GAP_STATUSES:
            return {"accepted": False, "error": "gap status is invalid", "allowed_statuses": sorted(RESEARCH_GAP_STATUSES)}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_refs = _workspace._invalid_workspace_refs(current, source_refs)
        if invalid_refs:
            return {"accepted": False, "error": "gap source_refs must exist in workspace", "invalid_source_refs": invalid_refs}
        if job is not None:
            invalid_refs = _workspace._refs_outside_job(job, source_refs)
            if invalid_refs:
                return {"accepted": False, "error": "gap source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = (
            await _workspace._invalid_job_evidence_ids(evidence_toolset, job, evidence_ids, source_refs=source_refs)
            if job is not None
            else await _workspace._invalid_workspace_evidence_ids(evidence_toolset, evidence_ids)
        )
        if invalid_evidence:
            error = (
                "gap evidence_ids must belong to selected source_refs and current job scope"
                if job is not None
                else "gap evidence_ids must exist in this run"
            )
            return {"accepted": False, "error": error, "invalid_evidence_ids": invalid_evidence}
        atom_refs, invalid_atom_refs = _workspace._resolve_workspace_atom_refs(current, args.get("atom_refs") or [])
        if invalid_atom_refs:
            return {"accepted": False, "error": "gap atom_refs must exist in workspace", "invalid_atom_refs": invalid_atom_refs}
        resolution_note = str(args.get("resolution_note") or "").strip()
        field_error = _workspace._semantic_field_error({"resolution_note": resolution_note}, ["resolution_note"])
        if field_error:
            return field_error
        if status == ResearchGapStatus.RESOLVED.value and not resolution_note:
            return {"accepted": False, "error": "resolved research gaps require resolution_note"}
        normalized_key = _gap_key(category, question)
        requested_gap_id = str(args.get("gap_id") or "").strip()
        current_gaps = [item for item in current.get("research_gaps") or [] if isinstance(item, dict)]
        existing_by_id = next(
            (item for item in current_gaps if requested_gap_id and str(item.get("gap_id") or "") == requested_gap_id),
            None,
        )
        existing_by_key = next(
            (
                item
                for item in current_gaps
                if str(item.get("normalized_key") or _gap_key(item.get("category") or "", item.get("question") or ""))
                == normalized_key
            ),
            None,
        )
        if existing_by_id is not None:
            existing_key = str(
                existing_by_id.get("normalized_key")
                or _gap_key(existing_by_id.get("category") or "", existing_by_id.get("question") or "")
            )
            if existing_key != normalized_key:
                return {
                    "accepted": False,
                    "error": "gap_id must update the same category and question",
                    "gap_id": requested_gap_id,
                    "expected_category": existing_by_id.get("category"),
                    "expected_question": existing_by_id.get("question"),
                }
        if existing_by_id is not None and existing_by_key is not None and existing_by_id is not existing_by_key:
            return {"accepted": False, "error": "gap identity conflicts with an existing research gap"}
        existing = existing_by_id or existing_by_key
        gap_id = str((existing or {}).get("gap_id") or _stable_id(
            requested_gap_id,
            question,
            len(current_gaps) + 1,
        ))
        gap = {
            "gap_id": gap_id,
            "question": question,
            "category": category,
            "severity": severity,
            "treatment_relevance": treatment_relevance,
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "atom_refs": atom_refs,
            "status": status,
            "resolution_note": resolution_note,
            "normalized_key": normalized_key,
            "created_at": _now(),
            "updated_at": _now(),
        }
        gap = _merge_research_gap(existing, gap) if isinstance(existing, dict) else gap
        if (
            gap.get("status") == ResearchGapStatus.RESOLVED.value
            and gap.get("severity") == ResearchGapSeverity.BLOCKING.value
            and not gap.get("evidence_ids")
            and not gap.get("atom_refs")
        ):
            return {"accepted": False, "error": "blocking research gaps cannot be resolved without evidence_ids or atom_refs"}
        if status == ResearchGapStatus.RESOLVED.value and evidence_toolset is not None:
            for evidence_id in evidence_ids:
                await evidence_toolset._mark_evidence_status(
                    evidence_id,
                    EVIDENCE_STATUS_SELECTED,
                    {
                        "selected_purpose": "research_gap_resolution",
                        "selected_role": "research_gap_resolution",
                        "selected_rationale": resolution_note,
                    },
                )
        replaced_gap_id = str((existing or {}).get("gap_id") or "")
        gaps = [
            item
            for item in current_gaps
            if str(item.get("gap_id") or "") != replaced_gap_id
            and str(item.get("normalized_key") or _gap_key(item.get("category") or "", item.get("question") or ""))
            != normalized_key
        ]
        gaps.append(gap)
        current["research_gaps"] = gaps
        _workspace._mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {
            "accepted": True,
            "gap_id": gap.get("gap_id") or gap_id,
            "gap_count": len(gaps),
            "status": gap.get("status"),
            "evidence_count": len(gap.get("evidence_ids") or []),
        }

    return record_research_gap_tool
