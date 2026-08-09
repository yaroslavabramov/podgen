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
from app.generation_v2.research_workspace_helpers import dedupe_normalized as _dedupe_normalized
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
        job_id = _stable_id(str(args.get("job_id") or ""), label, len(current.get("investigation_jobs") or []) + 1)
        existing_job = next(
            (dict(item) for item in current.get("investigation_jobs") or [] if str(item.get("job_id") or "") == job_id),
            {},
        )
        required_source_refs = (
            [str(item) for item in existing_job.get("source_refs") or []]
            if str(existing_job.get("corpus_handoff_id") or "").strip()
            else []
        )
        source_refs = _dedupe(
            [
                *required_source_refs,
                *[str(item) for item in args.get("source_refs") or [] if str(item).strip()],
            ]
        )
        invalid = [ref for ref in source_refs if ref not in source_order]
        if invalid:
            return {
                "accepted": False,
                "error": "source_refs must exist in this research workspace",
                "invalid_source_refs": invalid,
            }
        allow_broad_search = bool(args.get("allow_broad_search")) or bool(existing_job.get("allow_broad_search"))
        if not source_refs and not allow_broad_search:
            return {
                "accepted": False,
                "error": "research job requires source_refs unless allow_broad_search=true",
            }
        coverage_question_ids = [
            str(item) for item in existing_job.get("coverage_question_ids") or [] if str(item).strip()
        ]
        brief = current.get("corpus_brief") if isinstance(current.get("corpus_brief"), dict) else {}
        brief_questions = {
            str(item.get("question_id") or ""): str(item.get("question") or "").strip()
            for item in brief.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "").strip()
        }
        required_handoff_questions = [
            brief_questions[question_id] for question_id in coverage_question_ids if brief_questions.get(question_id)
        ]
        questions = _dedupe_normalized(
            [
                *required_handoff_questions,
                *[str(item).strip() for item in args.get("questions") or [] if str(item).strip()],
            ]
        )
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
        job = {
            "job_id": job_id,
            **(
                {"corpus_handoff_id": str(existing_job.get("corpus_handoff_id") or "")}
                if str(existing_job.get("corpus_handoff_id") or "").strip()
                else {}
            ),
            **({"coverage_question_ids": coverage_question_ids} if coverage_question_ids else {}),
            **(
                {
                    "corpus_handoff_question_assignments": list(
                        existing_job.get("corpus_handoff_question_assignments") or []
                    )
                }
                if existing_job.get("corpus_handoff_question_assignments")
                else {}
            ),
            **(
                {"corpus_handoff_finding_ids": list(existing_job.get("corpus_handoff_finding_ids") or [])}
                if existing_job.get("corpus_handoff_finding_ids")
                else {}
            ),
            **(
                {
                    "corpus_handoff_final_finding_ids": list(
                        existing_job.get("corpus_handoff_final_finding_ids") or []
                    )
                }
                if existing_job.get("corpus_handoff_final_finding_ids")
                else {}
            ),
            **(
                {
                    "corpus_handoff_candidate_finding_ids": list(
                        existing_job.get("corpus_handoff_candidate_finding_ids") or []
                    )
                }
                if existing_job.get("corpus_handoff_candidate_finding_ids")
                else {}
            ),
            **(
                {
                    "corpus_handoff_allowed_source_refs": list(
                        existing_job.get("corpus_handoff_allowed_source_refs") or []
                    )
                }
                if existing_job.get("corpus_handoff_allowed_source_refs")
                else {}
            ),
            **(
                {
                    "corpus_handoff_preferred_source_refs": list(
                        existing_job.get("corpus_handoff_preferred_source_refs") or []
                    )
                }
                if existing_job.get("corpus_handoff_preferred_source_refs")
                else {}
            ),
            **(
                {
                    "corpus_handoff_candidate_source_refs": list(
                        existing_job.get("corpus_handoff_candidate_source_refs") or []
                    )
                }
                if existing_job.get("corpus_handoff_candidate_source_refs")
                else {}
            ),
            **(
                {
                    "corpus_handoff_candidate_evidence_ids": list(
                        existing_job.get("corpus_handoff_candidate_evidence_ids") or []
                    )
                }
                if existing_job.get("corpus_handoff_candidate_evidence_ids")
                else {}
            ),
            **(
                {
                    "corpus_handoff_coverage_hint_ids": list(
                        existing_job.get("corpus_handoff_coverage_hint_ids") or []
                    )
                }
                if existing_job.get("corpus_handoff_coverage_hint_ids")
                else {}
            ),
            **(
                {"corpus_handoff_source_refs": list(existing_job.get("corpus_handoff_source_refs") or [])}
                if existing_job.get("corpus_handoff_source_refs")
                else {}
            ),
            **(
                {"corpus_handoff_evidence_ids": list(existing_job.get("corpus_handoff_evidence_ids") or [])}
                if existing_job.get("corpus_handoff_evidence_ids")
                else {}
            ),
            **(
                {"corpus_handoff_gaps": list(existing_job.get("corpus_handoff_gaps") or [])}
                if existing_job.get("corpus_handoff_gaps")
                else {}
            ),
            **(
                {"corpus_brief_outcomes": list(existing_job.get("corpus_brief_outcomes") or [])}
                if existing_job.get("corpus_brief_outcomes")
                else {}
            ),
            **(
                {"coverage_requirements": list(existing_job.get("coverage_requirements") or [])}
                if existing_job.get("coverage_requirements")
                else {}
            ),
            **(
                {"open_requirements": list(existing_job.get("open_requirements") or [])}
                if existing_job.get("open_requirements")
                else {}
            ),
            **(
                {"source_findings": list(existing_job.get("source_findings") or [])}
                if existing_job.get("source_findings")
                else {}
            ),
            **(
                {"source_gaps": list(existing_job.get("source_gaps") or [])}
                if existing_job.get("source_gaps")
                else {}
            ),
            **(
                {"source_cards": list(existing_job.get("source_cards") or [])}
                if existing_job.get("source_cards")
                else {}
            ),
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
            "starting_evidence_ids": _dedupe(
                [str(item) for item in existing_job.get("starting_evidence_ids") or [] if str(item).strip()]
            ),
            "evidence_ids": _dedupe(
                [str(item) for item in existing_job.get("evidence_ids") or [] if str(item).strip()]
            ),
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
        handoff_error = _corpus_brief_handoff_error(current, jobs)
        if handoff_error:
            return handoff_error
        summary = str(args.get("summary") or "").strip()
        field_error = _workspace._semantic_field_error({"plan_summary": summary}, ["plan_summary"])
        if field_error:
            return field_error
        current["status"] = "planned"
        current["plan_summary"] = summary
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "status": "planned", "job_count": len(jobs)}

    return upsert_research_job_tool, finalize_research_plan_tool


def _corpus_brief_handoff_error(
    workspace: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    brief = workspace.get("corpus_brief") if isinstance(workspace.get("corpus_brief"), dict) else {}
    question_by_id = {
        str(item.get("question_id") or ""): str(item.get("question") or "").strip()
        for item in brief.get("coverage_questions") or []
        if isinstance(item, dict) and str(item.get("question_id") or "").strip()
    }
    expected_question_ids = _dedupe(
        [
            str(question_id)
            for item in brief.get("research_handoff") or []
            if isinstance(item, dict)
            for question_id in item.get("question_ids") or []
            if str(question_id).strip()
        ]
    )
    if not expected_question_ids:
        return None

    represented: set[str] = set()
    for job in jobs:
        linked_ids = {
            str(question_id) for question_id in job.get("coverage_question_ids") or [] if str(question_id).strip()
        }
        represented.update(linked_ids.intersection(expected_question_ids))

    missing_question_ids = [question_id for question_id in expected_question_ids if question_id not in represented]
    if not missing_question_ids:
        return None
    return {
        "accepted": False,
        "error": "Every CorpusBrief handoff question must remain assigned to a research job",
        "missing_coverage_question_ids": missing_question_ids,
        "missing_questions": [
            question_by_id[question_id] for question_id in missing_question_ids if question_by_id.get(question_id)
        ],
    }
