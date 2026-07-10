from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.persistence_contracts import semantic_list_error
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_MEMO_RISK_REGISTER_LIMIT,
    RESEARCH_MEMO_SOURCE_COVERAGE_LIMIT,
)
from app.generation_v2.research_workspace_helpers import (
    active_atom_drafts as _active_atom_drafts,
    dedupe as _dedupe,
    draft_is_source_quality_note as _draft_is_source_quality_note,
    merge_atom_drafts_explicit as _merge_atom_drafts_explicit,
    now as _now,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus


def research_finalizer_tool_handlers(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
):
    async def merge_editorial_atoms_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        primary_atom_id = str(args.get("primary_atom_id") or "").strip()
        duplicate_atom_ids = _dedupe([str(item) for item in args.get("duplicate_atom_ids") or [] if str(item).strip()])
        if not primary_atom_id:
            return {"accepted": False, "error": "primary_atom_id is required"}
        if not duplicate_atom_ids:
            return {"accepted": False, "error": "duplicate_atom_ids is required"}
        if primary_atom_id in duplicate_atom_ids:
            return {"accepted": False, "error": "primary_atom_id cannot also be a duplicate_atom_id"}

        drafts = [dict(item) for item in current.get("atom_drafts") or [] if isinstance(item, dict)]
        by_id = {str(item.get("atom_id") or ""): item for item in drafts if str(item.get("atom_id") or "").strip()}
        primary = by_id.get(primary_atom_id)
        if primary is None:
            return {"accepted": False, "error": "primary_atom_id not found", "missing_atom_ids": [primary_atom_id]}
        missing = [atom_id for atom_id in duplicate_atom_ids if atom_id not in by_id]
        if missing:
            return {"accepted": False, "error": "duplicate_atom_id not found", "missing_atom_ids": missing}
        inactive = [
            atom_id
            for atom_id in [primary_atom_id, *duplicate_atom_ids]
            if str(by_id[atom_id].get("merged_into") or "").strip() or str(by_id[atom_id].get("status") or "") == "merged"
        ]
        if inactive:
            return {"accepted": False, "error": "cannot merge atom drafts that are already merged", "inactive_atom_ids": inactive}
        primary_kind = str(primary.get("kind") or "")
        cross_kind = [atom_id for atom_id in duplicate_atom_ids if str(by_id[atom_id].get("kind") or "") != primary_kind]
        if cross_kind:
            return {
                "accepted": False,
                "error": "merge_editorial_atoms only merges atom drafts with the same kind",
                "primary_kind": primary_kind,
                "cross_kind_atom_ids": cross_kind,
            }
        field_error = _workspace._semantic_field_error(
            args,
            [key for key in ["title", "claim", "explanation", "story_value", "risk", "treatment_role_hint"] if args.get(key)],
        )
        if field_error:
            return field_error

        merged = _merge_atom_drafts_explicit(primary, [by_id[atom_id] for atom_id in duplicate_atom_ids], args)
        merged_at = _now()
        updated_drafts: list[dict[str, Any]] = []
        for draft in drafts:
            atom_id = str(draft.get("atom_id") or "")
            if atom_id == primary_atom_id:
                updated_drafts.append(merged)
            elif atom_id in duplicate_atom_ids:
                retired = dict(draft)
                retired["merged_into"] = primary_atom_id
                retired["merged_at"] = merged_at
                retired["status"] = "merged"
                updated_drafts.append(retired)
            else:
                updated_drafts.append(draft)
        current["atom_drafts"] = updated_drafts
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {
            "accepted": True,
            "primary_atom_id": primary_atom_id,
            "merged_duplicate_count": len(duplicate_atom_ids),
            "evidence_count": len(merged.get("evidence_ids") or []),
            "source_ref_count": len(merged.get("source_refs") or []),
        }

    async def finalize_research_memo_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        active_content_drafts = [
            draft
            for draft in _active_atom_drafts(current.get("atom_drafts") or [])
            if not _draft_is_source_quality_note(draft)
        ]
        if not active_content_drafts:
            return {"accepted": False, "error": "Cannot finalize research memo without active content atom drafts"}
        title = str(args.get("title") or "").strip()
        thesis = str(args.get("thesis") or "").strip()
        research_synthesis = str(args.get("research_synthesis") or "").strip()
        if not title or not thesis or not research_synthesis:
            return {"accepted": False, "error": "title, thesis, and research_synthesis are required"}
        field_error = _workspace._semantic_field_error(
            {"title": title, "thesis": thesis, "research_synthesis": research_synthesis},
            ["title", "thesis", "research_synthesis"],
        )
        if field_error:
            return field_error
        source_coverage = [str(item).strip() for item in args.get("source_coverage") or [] if str(item).strip()]
        risk_register = [str(item).strip() for item in args.get("risk_register") or [] if str(item).strip()]
        for item in source_coverage:
            field_error = _workspace._semantic_field_error({"source_coverage_item": item}, ["source_coverage_item"])
            if field_error:
                return field_error
        for item in risk_register:
            field_error = _workspace._semantic_field_error({"risk_register_item": item}, ["risk_register_item"])
            if field_error:
                return field_error
        list_error = semantic_list_error(
            field="source_coverage",
            values=source_coverage,
            max_items=RESEARCH_MEMO_SOURCE_COVERAGE_LIMIT,
        )
        if list_error:
            return list_error
        list_error = semantic_list_error(
            field="risk_register",
            values=risk_register,
            max_items=RESEARCH_MEMO_RISK_REGISTER_LIMIT,
        )
        if list_error:
            return list_error
        current["memo_fields"] = {
            "title": title,
            "thesis": thesis,
            "research_synthesis": research_synthesis,
            "source_coverage": source_coverage,
            "risk_register": risk_register,
        }
        current["status"] = "finalized"
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current, status=V2ArtifactStatus.PASSED)
        return {"accepted": True, "status": "finalized", "atom_count": len(_active_atom_drafts(current.get("atom_drafts") or []))}

    return merge_editorial_atoms_tool, finalize_research_memo_tool
