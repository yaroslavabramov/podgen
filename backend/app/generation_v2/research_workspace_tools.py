from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.model_facing_record_parts import composite_record_parts
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
from app.generation_v2.research_workspace_prompt_payloads import prompt_atom_index_payload
from app.generation_v2.runtime import AgentTerminalResult, AgentTool
from app.generation_v2.schemas import ResearchWorkflowCompletion, SourceMap
from app.models.generation_run import GenerationRun
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref_for_record


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


def _full_job_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    jobs = _workspace_jobs(workspace)
    page = jobs[cursor : cursor + limit]
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
            "starting_evidence_ids": [str(item) for item in job.get("starting_evidence_ids") or []],
            "starting_evidence_count": len(job.get("starting_evidence_ids") or []),
            "evidence_ids": [str(item) for item in job.get("evidence_ids") or []],
            "evidence_count": len(job.get("evidence_ids") or []),
        }
        for job in page
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "jobs",
            "items": items,
            "page": _page_metadata(total=len(jobs), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_job_question_page(
    workspace: dict[str, Any],
    *,
    cursor: int,
    job_id: str,
    limit: int,
    section: str = "job_questions",
) -> dict[str, Any]:
    questions = [
        (str(job.get("job_id") or ""), str(question))
        for job in _workspace_jobs(workspace, job_id=job_id)
        for question in job.get("questions") or []
        if str(question).strip()
    ]
    page = questions[cursor : cursor + limit]
    items = [
        {"job_id": owner_job_id, "question_number": cursor + index, "question": question}
        for index, (owner_job_id, question) in enumerate(page, start=1)
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": section,
            "items": items,
            "page": _page_metadata(total=len(questions), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_job_question_outcome_page(
    workspace: dict[str, Any],
    *,
    cursor: int,
    job_id: str,
    limit: int,
) -> dict[str, Any]:
    outcomes = [
        (str(job.get("job_id") or ""), outcome)
        for job in _workspace_jobs(workspace, job_id=job_id)
        for outcome in job.get("question_outcomes") or []
        if isinstance(outcome, dict)
    ]
    page = outcomes[cursor : cursor + limit]
    items = [
        {
            "job_id": owner_job_id,
            "outcome_number": cursor + index,
            "question": str(outcome.get("question") or ""),
            "status": outcome.get("status"),
            "note": str(outcome.get("note") or ""),
            "evidence_ids": [str(item) for item in outcome.get("evidence_ids") or []],
            "evidence_count": len(outcome.get("evidence_ids") or []),
        }
        for index, (owner_job_id, outcome) in enumerate(page, start=1)
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "job_question_outcomes",
            "items": items,
            "page": _page_metadata(total=len(outcomes), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_research_gap_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    gaps = [item for item in workspace.get("research_gaps") or [] if isinstance(item, dict)]
    page = gaps[cursor : cursor + limit]
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
            "evidence_ids": [str(item) for item in gap.get("evidence_ids") or []],
            "evidence_count": len(gap.get("evidence_ids") or []),
        }
        for gap in page
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "research_gaps",
            "items": items,
            "page": _page_metadata(total=len(gaps), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_workspace_risk_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    risks = list(workspace.get("risk_register") or [])
    page = risks[cursor : cursor + limit]
    items = [
        {
            "risk": str(risk.get("risk") if isinstance(risk, dict) else risk),
            "severity": str(risk.get("severity") or "medium") if isinstance(risk, dict) else "medium",
            "source_refs": [str(item) for item in risk.get("source_refs") or []] if isinstance(risk, dict) else [],
            "atom_refs": [str(item) for item in risk.get("atom_refs") or []] if isinstance(risk, dict) else [],
            "evidence_ids": (
                [str(item) for item in risk.get("evidence_ids") or []]
                if isinstance(risk, dict)
                else []
            ),
            "evidence_count": len(risk.get("evidence_ids") or []) if isinstance(risk, dict) else 0,
        }
        for risk in page
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "workspace_risks",
            "items": items,
            "page": _page_metadata(total=len(risks), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_audit_feedback_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    feedback = [item for item in workspace.get("audit_repair_feedback") or [] if isinstance(item, dict)]
    page = feedback[cursor : cursor + limit]
    items = [
        {
            "target_step": item.get("target_step"),
            "severity": item.get("severity"),
            "description": str(item.get("description") or ""),
            "suggested_repair": str(item.get("suggested_repair") or ""),
            "atom_refs": [str(value) for value in item.get("atom_refs") or []],
            "source_refs": [str(value) for value in item.get("source_refs") or []],
            "evidence_ids": [str(value) for value in item.get("evidence_ids") or []],
            "evidence_count": len(item.get("evidence_ids") or []),
        }
        for item in page
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "audit_feedback",
            "items": items,
            "page": _page_metadata(total=len(feedback), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_corpus_brief_page(
    workspace: dict[str, Any],
    source_inventory: SourceMap,
    *,
    section: str,
    cursor: int,
    limit: int,
    include_evidence_ids: bool,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    brief = workspace.get("corpus_brief") if isinstance(workspace.get("corpus_brief"), dict) else {}
    context = build_source_ref_context(source_inventory)
    field_by_section = {
        "corpus_brief_questions": "coverage_questions",
        "corpus_brief_findings": "source_findings",
        "corpus_brief_outcomes": "coverage_outcomes",
        "corpus_brief_handoff": "research_handoff",
        "corpus_brief_source_sets": "source_sets",
        "corpus_brief_gap_records": "gap_records",
    }
    if section == "corpus_brief_summary":
        values = [{"summary": str(brief.get("summary") or "")}]
    elif section == "corpus_brief_intent":
        intent = brief.get("intent") if isinstance(brief.get("intent"), dict) else {}
        values = [dict(intent)]
    elif section == "corpus_brief_cautions":
        structured_gap_keys = {
            " ".join(str(item.get("description") or "").split()).casefold()
            for item in brief.get("gap_records") or []
            if isinstance(item, dict)
        }
        values = [
            {"caution": str(item)}
            for item in brief.get("cautions") or []
            if " ".join(str(item).split()).casefold() not in structured_gap_keys
        ]
    else:
        values = [item for item in brief.get(field_by_section[section]) or [] if isinstance(item, dict)]
        if job is not None and section == "corpus_brief_findings":
            values = [item for item in job.get("source_findings") or [] if isinstance(item, dict)]
        elif job is not None and section == "corpus_brief_outcomes":
            values = [item for item in job.get("corpus_brief_outcomes") or [] if isinstance(item, dict)]
        if section == "corpus_brief_source_sets":
            values = [item for item in values if str(item.get("set_kind") or "coverage") != "orientation"]
    rendered_values = [
        _model_safe_corpus_brief_item(
            item,
            section=section,
            context=context,
            include_evidence_ids=include_evidence_ids,
        )
        for item in values
    ]
    record_kind_by_section = {
        "corpus_brief_questions": "coverage_question",
        "corpus_brief_findings": "source_finding",
        "corpus_brief_outcomes": "coverage_outcome",
    }
    if section in record_kind_by_section:
        model_items = [
            part
            for item in rendered_values
            for part in composite_record_parts(
                item,
                record_kind=record_kind_by_section[section],
            )
        ]
        page_unit = "record_part"
    else:
        model_items = rendered_values
        page_unit = "record"
    items = model_items[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": section,
            "record_count": len(values),
            "items": items,
            "page": {
                **_page_metadata(total=len(model_items), cursor=cursor, returned=len(items)),
                "unit": page_unit,
            },
        },
        requested_limit=limit,
    )


def _model_safe_corpus_brief_item(
    item: dict[str, Any],
    *,
    section: str,
    context: Any,
    include_evidence_ids: bool,
) -> dict[str, Any]:
    payload = dict(item)
    if section == "corpus_brief_gap_records":
        payload["gap_ref"] = public_gap_ref_for_record(payload)
        payload.pop("gap_id", None)
        evidence_ids = [
            str(value) for value in payload.pop("evidence_ids", []) if str(value).strip()
        ]
        payload["evidence_count"] = len(evidence_ids)
        if include_evidence_ids:
            payload["evidence_ids"] = evidence_ids
    if section == "corpus_brief_findings":
        source_id = str(payload.pop("source_id", "") or "")
        payload.pop("source_version_ids", None)
        payload.pop("assessment_id", None)
        if source_id:
            payload["source_ref"] = source_ref_for_id(source_id, context)
        evidence_ids = [
            str(value) for value in payload.pop("evidence_ids", []) if str(value).strip()
        ]
        payload["evidence_count"] = len(evidence_ids)
        if include_evidence_ids:
            payload["evidence_ids"] = evidence_ids
        relations: list[dict[str, Any]] = []
        for relation in payload.get("source_relations") or []:
            if not isinstance(relation, dict):
                continue
            model_relation = dict(relation)
            related_source_id = str(model_relation.pop("related_source_id", "") or "")
            if related_source_id:
                model_relation["related_source_ref"] = source_ref_for_id(
                    related_source_id, context
                )
            relation_evidence_ids = [
                str(value)
                for value in model_relation.pop("evidence_ids", [])
                if str(value).strip()
            ]
            model_relation["evidence_count"] = len(relation_evidence_ids)
            if include_evidence_ids:
                model_relation["evidence_ids"] = relation_evidence_ids
            relations.append(model_relation)
        payload["source_relations"] = relations
    source_ids = payload.pop("source_ids", [])
    preferred_source_ids = payload.pop("preferred_source_ids", [])
    if source_ids:
        payload["source_refs"] = [source_ref_for_id(source_id, context) for source_id in source_ids]
    if preferred_source_ids:
        payload["preferred_source_refs"] = [
            source_ref_for_id(source_id, context) for source_id in preferred_source_ids
        ]
    return _model_safe_value(payload, replacements=context.ref_by_source_id)


def _model_safe_value(value: Any, *, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return model_preview_text(value, replacements=replacements, collapse_whitespace=False)
    if isinstance(value, list):
        return [_model_safe_value(item, replacements=replacements) for item in value]
    if isinstance(value, dict):
        return {key: _model_safe_value(item, replacements=replacements) for key, item in value.items()}
    return value


def _full_atom_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    atoms = active_atom_drafts(workspace.get("atom_drafts") or [])
    page = atoms[cursor : cursor + limit]
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
            "quote_refs": [str(item) for item in atom.get("quote_refs") or []],
            "anchor_refs": [str(item) for item in atom.get("anchor_refs") or []],
            "evidence_ids": [str(item) for item in atom.get("evidence_ids") or []],
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
    return fit_model_page(
        {
            "accepted": True,
            "section": "atom_drafts",
            "items": items,
            "page": _page_metadata(total=len(atoms), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _atom_index_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    atoms = active_atom_drafts(workspace.get("atom_drafts") or [])
    page = atoms[cursor : cursor + limit]
    items = [
        prompt_atom_index_payload(atom, atom_ref=f"A{cursor + index:02d}")
        for index, atom in enumerate(page, start=1)
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "atom_index",
            "items": items,
            "page": _page_metadata(total=len(atoms), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_observation_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    observations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for job in workspace.get("investigation_jobs") or []:
        if not isinstance(job, dict):
            continue
        observations.extend(
            (job, observation) for observation in job.get("observations") or [] if isinstance(observation, dict)
        )
    page = observations[cursor : cursor + limit]
    items = [
        {
            "observation_number": cursor + index,
            "job_id": job.get("job_id"),
            "job_label": job.get("label"),
            "source_refs": observation.get("source_refs") or [],
            "evidence_ids": [str(item) for item in observation.get("evidence_ids") or []],
            "evidence_count": len(observation.get("evidence_ids") or []),
            "has_evidence": bool(observation.get("evidence_ids") or []),
            "note": str(observation.get("note") or ""),
            "pending_questions": [str(item) for item in observation.get("pending_questions") or []],
            "created_at": observation.get("created_at"),
        }
        for index, (job, observation) in enumerate(page, start=1)
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "observations",
            "items": items,
            "page": _page_metadata(total=len(observations), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


def _full_current_job_observation_page(
    job: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    observations = [item for item in job.get("observations") or [] if isinstance(item, dict)]
    page = observations[cursor : cursor + limit]
    items = [
        {
            "observation_number": cursor + index,
            "source_refs": [str(item) for item in observation.get("source_refs") or []],
            "evidence_ids": [str(item) for item in observation.get("evidence_ids") or []],
            "evidence_count": len(observation.get("evidence_ids") or []),
            "note": str(observation.get("note") or ""),
            "pending_questions": [str(item) for item in observation.get("pending_questions") or []],
            "created_at": observation.get("created_at"),
        }
        for index, observation in enumerate(page, start=1)
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "current_job_observations",
            "items": items,
            "page": _page_metadata(total=len(observations), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
    )


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
        resolved_job_id = str(current_job_id or ((current.get("current_job") or {}).get("job_id")) or "")
        scoped_job = next(
            (
                item
                for item in current.get("investigation_jobs") or []
                if isinstance(item, dict) and str(item.get("job_id") or "") == resolved_job_id
            ),
            None,
        )
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
        if section in {
            "corpus_brief_summary",
            "corpus_brief_intent",
            "corpus_brief_cautions",
            "corpus_brief_questions",
            "corpus_brief_findings",
            "corpus_brief_outcomes",
            "corpus_brief_handoff",
            "corpus_brief_source_sets",
            "corpus_brief_gap_records",
        }:
            return _full_corpus_brief_page(
                current,
                source_inventory,
                section=section,
                cursor=cursor,
                limit=limit,
                include_evidence_ids=include_source_map_evidence_ids,
                job=scoped_job,
            )
        if section in {
            "current_job_questions",
            "current_job_observations",
            "current_job_starting_evidence",
        }:
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
            if section == "current_job_starting_evidence":
                evidence_ids = [str(item) for item in job.get("starting_evidence_ids") or [] if str(item).strip()]
                items = [{"evidence_id": evidence_id} for evidence_id in evidence_ids[cursor : cursor + limit]]
                return fit_model_page(
                    {
                        "accepted": True,
                        "section": section,
                        "items": items,
                        "page": _page_metadata(
                            total=len(evidence_ids),
                            cursor=cursor,
                            returned=len(items),
                        ),
                    },
                    requested_limit=limit,
                )
            if section == "current_job_questions":
                return _full_job_question_page(
                    current,
                    cursor=cursor,
                    job_id=resolved_job_id,
                    limit=limit,
                    section=section,
                )
            return _full_current_job_observation_page(job, cursor=cursor, limit=limit)
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
                    "summary": card.summary,
                    "useful_for": list(card.useful_for),
                    "gaps_or_cautions": list(card.gaps_or_cautions),
                    **(
                        {"evidence_ids": list(card.evidence_ids)}
                        if include_source_map_evidence_ids
                        else {"evidence_id_count": len(card.evidence_ids)}
                    ),
                }
                for card in page
            ]
            return fit_model_page(
                {
                    "accepted": True,
                    "section": section,
                    "items": items,
                    "page": _page_metadata(total=len(cards), cursor=cursor, returned=len(items)),
                },
                requested_limit=limit,
            )
        if section == "atom_drafts":
            return _full_atom_page(current, cursor=cursor, limit=limit)
        if section == "atom_index":
            return _atom_index_page(current, cursor=cursor, limit=limit)
        if section == "observations":
            return _full_observation_page(current, cursor=cursor, limit=limit)
        if section == "jobs":
            return _full_job_page(current, cursor=cursor, limit=limit)
        if section == "job_questions":
            return _full_job_question_page(current, cursor=cursor, job_id=job_id, limit=limit)
        if section == "job_question_outcomes":
            return _full_job_question_outcome_page(current, cursor=cursor, job_id=job_id, limit=limit)
        if section == "research_gaps":
            return _full_research_gap_page(current, cursor=cursor, limit=limit)
        if section == "workspace_risks":
            return _full_workspace_risk_page(current, cursor=cursor, limit=limit)
        if section == "audit_feedback":
            return _full_audit_feedback_page(current, cursor=cursor, limit=limit)
        return {
            "accepted": False,
            "error": (
                "section must be sources, jobs, job_questions, job_question_outcomes, atom_index, atom_drafts, "
                "research_gaps, workspace_risks, audit_feedback, observations, current_job_questions, "
                "current_job_observations, current_job_starting_evidence, or a source_map_* section"
            ),
        }

    return AgentTool(
        name="list_research_workspace_items",
        description=(
            "Page persisted research/source-map items when initial context is truncated or omitted. "
            "Validated composite CorpusBrief questions, findings, and outcomes may be returned as ordered "
            "record parts keyed by owner_ref and part_kind. Semantic scalars remain whole; an individually "
            "oversized scalar returns an explicit validation error. Follow page.next_cursor."
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
                        "atom_index",
                        "atom_drafts",
                        "research_gaps",
                        "workspace_risks",
                        "audit_feedback",
                        "observations",
                        "current_job_questions",
                        "current_job_observations",
                        "current_job_starting_evidence",
                        "corpus_brief_summary",
                        "corpus_brief_intent",
                        "corpus_brief_cautions",
                        "corpus_brief_questions",
                        "corpus_brief_findings",
                        "corpus_brief_outcomes",
                        "corpus_brief_handoff",
                        "corpus_brief_source_sets",
                        "corpus_brief_gap_records",
                        "source_map_cards",
                        "source_map_assessments",
                        "source_map_quotes",
                        "source_map_clusters",
                        "source_map_relationships",
                        "source_map_gaps",
                        "source_map_gap_records",
                    ],
                },
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                "job_id": {
                    "type": "string",
                    "description": (
                        "Use the relevant job_id for job_questions or job_question_outcomes; "
                        "pass null for sections that do not use it."
                    ),
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

    async def planner_resume_loader() -> AgentTerminalResult | None:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        if current.get("status") != "planned":
            return None
        return AgentTerminalResult(
            accepted=True,
            completion=ResearchWorkflowCompletion(
                ready=True,
                summary=str(current.get("plan_summary") or "Research plan already persisted."),
                notes=[],
            ),
        )

    async def job_resume_loader() -> AgentTerminalResult | None:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job = _workspace._workspace_job(current, str(current_job_id or ""))
        if job is None or job.get("status") not in {"mapped", "deferred"}:
            return None
        deferred_reason = str(job.get("deferred_reason") or "").strip()
        return AgentTerminalResult(
            accepted=True,
            completion=ResearchWorkflowCompletion(
                ready=True,
                summary=str(job.get("summary") or "Research job already persisted."),
                notes=[deferred_reason] if deferred_reason else [],
            ),
        )

    async def finalizer_resume_loader() -> AgentTerminalResult | None:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        memo_fields = current.get("memo_fields") if isinstance(current.get("memo_fields"), dict) else {}
        if current.get("status") != "finalized" or not memo_fields:
            return None
        return AgentTerminalResult(
            accepted=True,
            completion=ResearchWorkflowCompletion(
                ready=True,
                summary=str(memo_fields.get("title") or "Research memo already persisted."),
                notes=[],
            ),
        )

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
        tools.extend(
            research_planner_tools(
                upsert_research_job_tool,
                finalize_research_plan_tool,
                terminal_resume_loader=planner_resume_loader,
            )
        )
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
                terminal_resume_loader=job_resume_loader,
            )
        )
    if mode == "finalizer":
        tools.extend(
            research_finalizer_tools(
                merge_editorial_atoms_tool,
                record_research_risk_tool,
                record_research_gap_tool,
                finalize_research_memo_tool,
                terminal_resume_loader=finalizer_resume_loader,
            )
        )
    return tools
