from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.persistence_contracts import semantic_list_error
from app.generation_v2.research_kinds import ALLOWED_RESEARCH_ATOM_KINDS, ATOM_KIND_GUIDANCE
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_ANCHOR_REF_LIMIT,
    RESEARCH_QUOTE_REF_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_helpers import (
    atom_kind as _atom_kind,
    confidence as _confidence,
    coverage_status as _coverage_status,
    dedupe as _dedupe,
    looks_like_source_quality_note as _looks_like_source_quality_note,
    now as _now,
    stable_id as _stable_id,
    story_weight as _story_weight,
)
from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_SELECTED
from app.models.generation_run import GenerationRun


def upsert_editorial_atom_handler(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    source_inventory: SourceMap,
    evidence_toolset: Any | None,
    current_job_id: str | None,
):
    async def upsert_editorial_atom_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace._workspace_job(current, job_id)
        if job is None:
            return {"accepted": False, "error": "Unknown research job", "job_id": job_id}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        if not source_refs:
            return {"accepted": False, "error": "upsert_editorial_atom requires source_refs"}
        invalid_workspace_refs = _workspace._invalid_workspace_refs(current, source_refs)
        if invalid_workspace_refs:
            return {
                "accepted": False,
                "error": "atom source_refs must exist in the research workspace",
                "invalid_source_refs": invalid_workspace_refs,
            }
        invalid_refs = _workspace._refs_outside_job(job, source_refs)
        if invalid_refs:
            return {"accepted": False, "error": "atom source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        if not evidence_ids:
            return {"accepted": False, "error": "upsert_editorial_atom requires at least one evidence_id"}
        if evidence_toolset is None:
            return {"accepted": False, "error": "upsert_editorial_atom requires evidence validation tools"}
        invalid_evidence = await _workspace._invalid_job_evidence_ids(evidence_toolset, job, evidence_ids, source_refs=source_refs)
        if invalid_evidence:
            return {"accepted": False, "error": "atom evidence_ids must belong to selected source_refs and current job scope", "invalid_evidence_ids": invalid_evidence}
        source_ids = await _workspace._source_ids_for_refs(evidence_toolset, source_inventory, source_refs)
        if not source_ids:
            return {"accepted": False, "error": "source_refs could not be resolved"}
        kind = _atom_kind(str(args.get("kind") or ""))
        if kind is None:
            return {
                "accepted": False,
                "error": "atom kind must be one of the supported research finding kinds",
                "allowed_kinds": sorted(ALLOWED_RESEARCH_ATOM_KINDS),
                "kind_guidance": ATOM_KIND_GUIDANCE,
            }
        title = str(args.get("title") or "").strip()
        claim = str(args.get("claim") or "").strip()
        story_value = str(args.get("story_value") or "").strip()
        if not title or not claim or not story_value:
            return {"accepted": False, "error": "title, claim, and story_value are required"}
        field_error = _workspace._semantic_field_error(
            {
                "title": title,
                "claim": claim,
                "explanation": str(args.get("explanation") or claim).strip(),
                "story_value": story_value,
                "risk": str(args.get("risk") or "medium").strip(),
                "treatment_role_hint": str(args.get("treatment_role_hint") or "").strip(),
            },
            ["title", "claim", "explanation", "story_value", "risk", "treatment_role_hint"],
        )
        if field_error:
            return field_error
        if _looks_like_source_quality_note(title, claim, story_value, str(args.get("explanation") or "")):
            return {
                "accepted": False,
                "error": "source-quality notes are not editorial atoms; use record_research_risk or record_research_gap",
            }
        for evidence_id in evidence_ids:
            await evidence_toolset._mark_evidence_status(
                evidence_id,
                EVIDENCE_STATUS_SELECTED,
                {
                    "selected_purpose": f"research_atom:{kind}",
                    "selected_role": "research_atom",
                    "selected_rationale": title,
                },
            )
        atom_id = _stable_id(str(args.get("atom_id") or ""), title, len(current.get("atom_drafts") or []) + 1)
        story_weight = _story_weight(str(args.get("story_weight") or ""), kind=kind)
        treatment_role_hint = str(args.get("treatment_role_hint") or "").strip()
        quote_refs = [str(item).strip() for item in args.get("quote_refs") or [] if str(item).strip()]
        anchor_refs = [str(item).strip() for item in args.get("anchor_refs") or [] if str(item).strip()]
        for field_name, values, limit, max_items in (
            ("quote_refs", quote_refs, SEMANTIC_FIELD_LIMITS["quote_ref"], RESEARCH_QUOTE_REF_LIMIT),
            ("anchor_refs", anchor_refs, SEMANTIC_FIELD_LIMITS["anchor_ref"], RESEARCH_ANCHOR_REF_LIMIT),
        ):
            list_error = semantic_list_error(field=field_name, values=values, max_items=max_items, item_limit=limit)
            if list_error:
                return list_error
        draft = {
            "atom_id": atom_id,
            "job_id": job_id,
            "kind": kind,
            "title": title,
            "claim": claim,
            "explanation": str(args.get("explanation") or claim).strip(),
            "source_refs": source_refs,
            "source_ids": source_ids,
            "quote_refs": quote_refs,
            "story_value": story_value,
            "anchor_refs": anchor_refs,
            "evidence_ids": evidence_ids,
            "confidence": _confidence(args.get("confidence")),
            "risk": str(args.get("risk") or "medium").strip() or "medium",
            "source_coverage_status": _coverage_status(str(args.get("source_coverage_status") or "")),
            "story_weight": story_weight,
            "treatment_role_hint": treatment_role_hint,
            "created_at": _now(),
            "updated_at": _now(),
        }
        current["atom_drafts"] = [item for item in current.get("atom_drafts") or [] if item.get("atom_id") != atom_id]
        current["atom_drafts"].append(draft)
        job["atom_ids"] = _dedupe([*(job.get("atom_ids") or []), atom_id])
        job["evidence_ids"] = _dedupe([*(job.get("evidence_ids") or []), *evidence_ids])
        job["updated_at"] = _now()
        _workspace._replace_workspace_job(current, job)
        _workspace._mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "atom_id": atom_id, "job_id": job_id, "evidence_count": len(evidence_ids), "atom_count": len(current["atom_drafts"])}

    return upsert_editorial_atom_tool
