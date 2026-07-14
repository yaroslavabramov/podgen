from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.persistence_contracts import semantic_list_error
from app.generation_v2.research_kinds import ALLOWED_RESEARCH_ATOM_KINDS
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_QUESTION_LIMIT,
    RESEARCH_REQUIRED_ATOM_KIND_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_helpers import dedupe as _dedupe
from app.generation_v2.research_workspace_helpers import now as _now
from app.generation_v2.research_workspace_helpers import stable_id as _stable_id
from app.models.generation_run import GenerationRun


def research_planner_tool_handlers(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
) -> tuple:
    async def upsert_research_job_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        source_order = set(str(item) for item in current.get("source_order") or [])
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid = [ref for ref in source_refs if ref not in source_order]
        if invalid:
            return {
                "accepted": False,
                "error": "source_refs must exist in this research workspace",
                "invalid_source_refs": invalid,
            }
        allow_broad_search = bool(args.get("allow_broad_search"))
        if not source_refs and not allow_broad_search:
            return {"accepted": False, "error": "research job requires source_refs unless allow_broad_search=true"}
        label = str(args.get("label") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not label or not purpose:
            return {"accepted": False, "error": "label and purpose are required"}
        story_job = str(args.get("story_job") or "general").strip() or "general"
        priority = str(args.get("priority") or "normal").strip() or "normal"
        field_error = _workspace._semantic_field_error(
            {"label": label, "story_job": story_job, "purpose": purpose, "priority": priority},
            ["label", "story_job", "purpose", "priority"],
        )
        if field_error:
            return field_error
        questions = [str(item).strip() for item in args.get("questions") or [] if str(item).strip()]
        list_error = semantic_list_error(
            field="questions",
            values=questions,
            max_items=RESEARCH_QUESTION_LIMIT,
            item_limit=SEMANTIC_FIELD_LIMITS["question"],
        )
        if list_error:
            return list_error
        if not questions:
            return {"accepted": False, "error": "at least one investigation question is required"}
        invalid_required_kinds = [
            str(item)
            for item in args.get("required_atom_kinds") or []
            if str(item).strip() and str(item).strip() not in ALLOWED_RESEARCH_ATOM_KINDS
        ]
        if invalid_required_kinds:
            return {
                "accepted": False,
                "error": "required_atom_kinds must use supported research atom kinds",
                "invalid_required_atom_kinds": invalid_required_kinds,
                "allowed_kinds": sorted(ALLOWED_RESEARCH_ATOM_KINDS),
            }
        required_atom_kinds = [
            str(item)
            for item in args.get("required_atom_kinds") or []
            if str(item).strip() in ALLOWED_RESEARCH_ATOM_KINDS
        ]
        list_error = semantic_list_error(
            field="required_atom_kinds",
            values=required_atom_kinds,
            max_items=RESEARCH_REQUIRED_ATOM_KIND_LIMIT,
        )
        if list_error:
            return list_error
        job_id = _stable_id(str(args.get("job_id") or ""), label, len(current.get("investigation_jobs") or []) + 1)
        job = {
            "job_id": job_id,
            "label": label,
            "story_job": story_job,
            "purpose": purpose,
            "source_refs": source_refs,
            "questions": questions,
            "required_atom_kinds": required_atom_kinds,
            "priority": priority,
            "allow_broad_search": allow_broad_search,
            "status": "planned",
            "observations": [],
            "atom_ids": [],
            "evidence_ids": [],
            "summary": "",
            "deferred_reason": "",
            "question_outcomes": [],
            "attempt": 0,
            "updated_at": _now(),
        }
        jobs = [item for item in current.get("investigation_jobs") or [] if item.get("job_id") != job_id]
        jobs.append(job)
        current["investigation_jobs"] = jobs
        current["status"] = "planning"
        current["research_questions"] = _dedupe([*(current.get("research_questions") or []), *questions])
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {
            "accepted": True,
            "job_id": job_id,
            "planned_job_count": len(jobs),
            "source_count": len(source_refs),
        }

    async def finalize_research_plan_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        jobs = [dict(item) for item in current.get("investigation_jobs") or []]
        if not jobs:
            return {"accepted": False, "error": "Cannot finalize research plan without investigation jobs"}
        summary = str(args.get("summary") or "").strip()
        field_error = _workspace._semantic_field_error({"plan_summary": summary}, ["plan_summary"])
        if field_error:
            return field_error
        current["status"] = "planned"
        current["plan_summary"] = summary
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "status": "planned", "job_count": len(jobs)}

    return upsert_research_job_tool, finalize_research_plan_tool
