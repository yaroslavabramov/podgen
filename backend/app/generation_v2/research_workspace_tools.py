from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.research_workspace_tool_specs import (
    research_finalizer_tools,
    research_job_tools,
    research_planner_tools,
)
from app.generation_v2.research_workspace_atom_tools import upsert_editorial_atom_handler
from app.generation_v2.research_workspace_finalizer_tools import research_finalizer_tool_handlers
from app.generation_v2.research_workspace_gap_tools import record_research_gap_handler
from app.generation_v2.research_workspace_job_tools import (
    finish_research_job_handler,
    record_research_observation_handler,
)
from app.generation_v2.research_workspace_planner_tools import research_planner_tool_handlers
from app.generation_v2.research_workspace_risk_tools import record_research_risk_handler
from app.generation_v2.research_source_map_prompting import source_map_research_page
from app.generation_v2.research_workspace_helpers import active_atom_drafts
from app.generation_v2.research_workspace_prompt_payloads import current_job_prompt_payload
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceMap
from app.models.generation_run import GenerationRun
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id


def _page_metadata(*, total: int, cursor: int, returned: int) -> dict[str, Any]:
    next_offset = cursor + returned
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - next_offset),
        "cursor": str(cursor),
        "next_cursor": str(next_offset) if next_offset < total else "",
    }


def _workspace_jobs(workspace: dict[str, Any], *, job_id: str = "") -> list[dict[str, Any]]:
    jobs = [item for item in workspace.get("investigation_jobs") or [] if isinstance(item, dict)]
    if job_id:
        return [item for item in jobs if str(item.get("job_id") or "") == job_id]
    return jobs


def _full_job_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    jobs = _workspace_jobs(workspace)
    page = jobs[cursor : cursor + 1]
    items = [
        {
            "job_id": job.get("job_id"),
            "label": str(job.get("label") or ""),
            "story_job": str(job.get("story_job") or ""),
            "purpose": str(job.get("purpose") or ""),
            "source_refs": [str(item) for item in job.get("source_refs") or []],
            "required_atom_kinds": [str(item) for item in job.get("required_atom_kinds") or []],
            "priority": str(job.get("priority") or ""),
            "allow_broad_search": bool(job.get("allow_broad_search")),
            "status": job.get("status"),
            "attempt": int(job.get("attempt") or 0),
            "summary": str(job.get("summary") or ""),
            "deferred_reason": str(job.get("deferred_reason") or ""),
            "resume_reason": str(job.get("resume_reason") or ""),
            "repair_for_gap_id": job.get("repair_for_gap_id"),
            "question_count": len(job.get("questions") or []),
            "question_outcome_count": len(job.get("question_outcomes") or []),
            "observation_count": len(job.get("observations") or []),
            "atom_count": len(job.get("atom_ids") or []),
            "evidence_count": len(job.get("evidence_ids") or []),
        }
        for job in page
    ]
    return {
        "accepted": True,
        "section": "jobs",
        "items": items,
        "page": _page_metadata(total=len(jobs), cursor=cursor, returned=len(items)),
    }


def _full_job_question_page(
    workspace: dict[str, Any],
    *,
    cursor: int,
    job_id: str,
) -> dict[str, Any]:
    questions = [
        (str(job.get("job_id") or ""), str(question))
        for job in _workspace_jobs(workspace, job_id=job_id)
        for question in job.get("questions") or []
        if str(question).strip()
    ]
    page = questions[cursor : cursor + 1]
    items = [
        {"job_id": owner_job_id, "question_number": cursor + index, "question": question}
        for index, (owner_job_id, question) in enumerate(page, start=1)
    ]
    return {
        "accepted": True,
        "section": "job_questions",
        "items": items,
        "page": _page_metadata(total=len(questions), cursor=cursor, returned=len(items)),
    }


def _full_job_question_outcome_page(
    workspace: dict[str, Any],
    *,
    cursor: int,
    job_id: str,
) -> dict[str, Any]:
    outcomes = [
        (str(job.get("job_id") or ""), outcome)
        for job in _workspace_jobs(workspace, job_id=job_id)
        for outcome in job.get("question_outcomes") or []
        if isinstance(outcome, dict)
    ]
    page = outcomes[cursor : cursor + 1]
    items = [
        {
            "job_id": owner_job_id,
            "outcome_number": cursor + index,
            "question": str(outcome.get("question") or ""),
            "status": outcome.get("status"),
            "note": str(outcome.get("note") or ""),
            "evidence_count": len(outcome.get("evidence_ids") or []),
        }
        for index, (owner_job_id, outcome) in enumerate(page, start=1)
    ]
    return {
        "accepted": True,
        "section": "job_question_outcomes",
        "items": items,
        "page": _page_metadata(total=len(outcomes), cursor=cursor, returned=len(items)),
    }


def _full_research_gap_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    gaps = [item for item in workspace.get("research_gaps") or [] if isinstance(item, dict)]
    page = gaps[cursor : cursor + 1]
    items = [
        {
            "gap_id": gap.get("gap_id"),
            "question": str(gap.get("question") or ""),
            "category": gap.get("category"),
            "severity": gap.get("severity"),
            "treatment_relevance": gap.get("treatment_relevance"),
            "status": gap.get("status"),
            "source_refs": [str(item) for item in gap.get("source_refs") or []],
            "atom_refs": [str(item) for item in gap.get("atom_refs") or []],
            "resolution_note": str(gap.get("resolution_note") or ""),
            "evidence_count": len(gap.get("evidence_ids") or []),
        }
        for gap in page
    ]
    return {
        "accepted": True,
        "section": "research_gaps",
        "items": items,
        "page": _page_metadata(total=len(gaps), cursor=cursor, returned=len(items)),
    }


def _full_workspace_risk_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    risks = list(workspace.get("risk_register") or [])
    page = risks[cursor : cursor + 1]
    items = [
        {
            "risk": str(risk.get("risk") if isinstance(risk, dict) else risk),
            "severity": str(risk.get("severity") or "medium") if isinstance(risk, dict) else "medium",
            "source_refs": [str(item) for item in risk.get("source_refs") or []]
            if isinstance(risk, dict)
            else [],
            "atom_refs": [str(item) for item in risk.get("atom_refs") or []]
            if isinstance(risk, dict)
            else [],
            "evidence_count": len(risk.get("evidence_ids") or []) if isinstance(risk, dict) else 0,
        }
        for risk in page
    ]
    return {
        "accepted": True,
        "section": "workspace_risks",
        "items": items,
        "page": _page_metadata(total=len(risks), cursor=cursor, returned=len(items)),
    }


def _full_audit_feedback_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    feedback = [item for item in workspace.get("audit_repair_feedback") or [] if isinstance(item, dict)]
    page = feedback[cursor : cursor + 1]
    items = [
        {
            "target_step": item.get("target_step"),
            "severity": item.get("severity"),
            "description": str(item.get("description") or ""),
            "suggested_repair": str(item.get("suggested_repair") or ""),
            "atom_refs": [str(value) for value in item.get("atom_refs") or []],
            "source_refs": [str(value) for value in item.get("source_refs") or []],
            "evidence_count": len(item.get("evidence_ids") or []),
        }
        for item in page
    ]
    return {
        "accepted": True,
        "section": "audit_feedback",
        "items": items,
        "page": _page_metadata(total=len(feedback), cursor=cursor, returned=len(items)),
    }


def _full_atom_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    atoms = active_atom_drafts(workspace.get("atom_drafts") or [])
    page = atoms[cursor : cursor + 1]
    items = [
        {
            "atom_ref": f"A{cursor + index:02d}",
            "atom_id": atom.get("atom_id"),
            "job_id": atom.get("job_id"),
            "kind": atom.get("kind"),
            "title": str(atom.get("title") or ""),
            "claim": str(atom.get("claim") or ""),
            "explanation": str(atom.get("explanation") or ""),
            "story_value": str(atom.get("story_value") or ""),
            "source_refs": atom.get("source_refs") or [],
            "evidence_count": len(atom.get("evidence_ids") or []),
            "has_evidence": bool(atom.get("evidence_ids") or []),
            "confidence": atom.get("confidence"),
            "risk": str(atom.get("risk") or ""),
            "source_coverage_status": atom.get("source_coverage_status"),
            "story_weight": atom.get("story_weight"),
            "treatment_role_hint": str(atom.get("treatment_role_hint") or ""),
        }
        for index, atom in enumerate(page, start=1)
    ]
    return {
        "accepted": True,
        "section": "atom_drafts",
        "items": items,
        "page": _page_metadata(total=len(atoms), cursor=cursor, returned=len(items)),
    }


def _full_observation_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    observations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for job in workspace.get("investigation_jobs") or []:
        if not isinstance(job, dict):
            continue
        observations.extend(
            (job, observation)
            for observation in job.get("observations") or []
            if isinstance(observation, dict)
        )
    page = observations[cursor : cursor + 1]
    items = [
        {
            "observation_number": cursor + index,
            "job_id": job.get("job_id"),
            "job_label": job.get("label"),
            "source_refs": observation.get("source_refs") or [],
            "evidence_count": len(observation.get("evidence_ids") or []),
            "has_evidence": bool(observation.get("evidence_ids") or []),
            "note": str(observation.get("note") or ""),
            "pending_questions": [str(item) for item in observation.get("pending_questions") or []],
            "created_at": observation.get("created_at"),
        }
        for index, (job, observation) in enumerate(page, start=1)
    ]
    return {
        "accepted": True,
        "section": "observations",
        "items": items,
        "page": _page_metadata(total=len(observations), cursor=cursor, returned=len(items)),
    }


def _research_workspace_page_tool(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    source_inventory: SourceMap,
    source_map: SourceMap,
    current_job_id: str | None,
    include_source_map_evidence_ids: bool,
) -> AgentTool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from app.generation_v2 import research_workspace as workspace_api

        current = await workspace_api.load_research_workspace(db, run.id) or workspace
        section = str(args.get("section") or "")
        cursor_text = str(args.get("cursor") or "0")
        cursor = int(cursor_text) if cursor_text.isdigit() else 0
        cursor = max(0, cursor)
        limit = max(1, min(int(args.get("limit") or 20), 40))
        job_id = str(args.get("job_id") or "")
        if section.startswith("source_map_"):
            try:
                payload = source_map_research_page(
                    source_map,
                    source_inventory,
                    section=section,
                    cursor=cursor,
                    limit=limit,
                    include_evidence_ids=include_source_map_evidence_ids,
                )
            except ValueError as exc:
                return {"accepted": False, "error": str(exc)}
            return {"accepted": True, **payload}
        if section in {"current_job_questions", "current_job_observations"}:
            resolved_job_id = str(
                current_job_id or ((current.get("current_job") or {}).get("job_id")) or ""
            )
            job = next(
                (
                    item
                    for item in current.get("investigation_jobs") or []
                    if str(item.get("job_id") or "") == resolved_job_id
                ),
                None,
            )
            if not isinstance(job, dict):
                return {"accepted": False, "error": "Current research job was not found"}
            job_payload = current_job_prompt_payload(
                job,
                question_cursor=cursor if section == "current_job_questions" else 0,
                max_questions=limit if section == "current_job_questions" else 1,
                observation_cursor=cursor if section == "current_job_observations" else 0,
                max_observations=limit if section == "current_job_observations" else 1,
            )
            page_key = "questions" if section == "current_job_questions" else "observations"
            return {
                "accepted": True,
                "section": section,
                "items": job_payload.get(page_key) or [],
                "page": (job_payload.get("pages") or {}).get(page_key) or {},
            }
        if section == "sources":
            cards = list(source_inventory.source_cards)
            page = cards[cursor : cursor + limit]
            context = build_source_ref_context(source_inventory)
            items = [
                {
                    "source_ref": source_ref_for_id(card.source_id, context),
                    "title": card.title,
                    "source_type": card.source_type,
                    "domain": card.domain,
                    "summary_preview": card.summary[:500],
                    "summary_chars": len(card.summary),
                    "summary_truncated": len(card.summary) > 500,
                }
                for card in page
            ]
            next_offset = cursor + len(items)
            return {
                "accepted": True,
                "section": section,
                "items": items,
                "page": {
                    "total_count": len(cards),
                    "returned_count": len(items),
                    "omitted_count": max(0, len(cards) - next_offset),
                    "cursor": str(cursor),
                    "next_cursor": str(next_offset) if next_offset < len(cards) else "",
                },
            }
        if section == "atom_drafts":
            return _full_atom_page(current, cursor=cursor)
        if section == "observations":
            return _full_observation_page(current, cursor=cursor)
        if section == "jobs":
            return _full_job_page(current, cursor=cursor)
        if section == "job_questions":
            return _full_job_question_page(current, cursor=cursor, job_id=job_id)
        if section == "job_question_outcomes":
            return _full_job_question_outcome_page(current, cursor=cursor, job_id=job_id)
        if section == "research_gaps":
            return _full_research_gap_page(current, cursor=cursor)
        if section == "workspace_risks":
            return _full_workspace_risk_page(current, cursor=cursor)
        if section == "audit_feedback":
            return _full_audit_feedback_page(current, cursor=cursor)
        return {
            "accepted": False,
            "error": (
                "section must be sources, jobs, job_questions, job_question_outcomes, atom_drafts, "
                "research_gaps, workspace_risks, audit_feedback, observations, current_job_questions, "
                "current_job_observations, or a source_map_* section"
            ),
        }

    return AgentTool(
        name="list_research_workspace_items",
        description=(
            "Page persisted research/source-map items when initial context is truncated or omitted. "
            "Jobs, questions/outcomes, gaps, risks, audit feedback, atom drafts, observations, and "
            "SourceMap sections return one full validated semantic item per page."
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": [
                        "sources",
                        "jobs",
                        "job_questions",
                        "job_question_outcomes",
                        "atom_drafts",
                        "research_gaps",
                        "workspace_risks",
                        "audit_feedback",
                        "observations",
                        "current_job_questions",
                        "current_job_observations",
                        "source_map_cards",
                        "source_map_quotes",
                        "source_map_clusters",
                        "source_map_relationships",
                        "source_map_gaps",
                    ],
                },
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                "job_id": {
                    "type": "string",
                    "description": "Optional job_id filter for job_questions or job_question_outcomes.",
                },
            },
            "required": ["section", "cursor", "limit"],
        },
        handler=handler,
    )


def build_research_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    source_inventory: SourceMap,
    source_map: SourceMap | None = None,
    evidence_toolset: Any | None = None,
    mode: str,
    current_job_id: str | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = []
    if mode in {"planner", "job", "finalizer"}:
        tools.append(
            _research_workspace_page_tool(
                db=db,
                run=run,
                workspace=workspace,
                source_inventory=source_inventory,
                source_map=source_map or SourceMap(),
                current_job_id=current_job_id,
                include_source_map_evidence_ids=mode == "job",
            )
        )

    if mode == "planner":
        upsert_research_job_tool, finalize_research_plan_tool = research_planner_tool_handlers(
            db=db,
            run=run,
            workspace=workspace,
        )
        tools.extend(research_planner_tools(upsert_research_job_tool, finalize_research_plan_tool))
    record_research_risk_tool = record_research_risk_handler(
        db=db,
        run=run,
        workspace=workspace,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    record_research_gap_tool = record_research_gap_handler(
        db=db,
        run=run,
        workspace=workspace,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    finish_research_job_tool = finish_research_job_handler(
        db=db,
        run=run,
        workspace=workspace,
        current_job_id=current_job_id,
    )
    record_research_observation_tool = record_research_observation_handler(
        db=db,
        run=run,
        workspace=workspace,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    upsert_editorial_atom_tool = upsert_editorial_atom_handler(
        db=db,
        run=run,
        workspace=workspace,
        source_inventory=source_inventory,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    merge_editorial_atoms_tool, finalize_research_memo_tool = research_finalizer_tool_handlers(
        db=db,
        run=run,
        workspace=workspace,
    )
    if mode == "job":
        tools.extend(
            research_job_tools(
                record_research_observation_tool,
                upsert_editorial_atom_tool,
                finish_research_job_tool,
                record_research_risk_tool,
                record_research_gap_tool,
            )
        )
    if mode == "finalizer":
        tools.extend(
            research_finalizer_tools(
                merge_editorial_atoms_tool,
                record_research_risk_tool,
                record_research_gap_tool,
                finalize_research_memo_tool,
            )
        )
    return tools
