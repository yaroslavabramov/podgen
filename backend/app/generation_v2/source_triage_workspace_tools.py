from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as _workspace
from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.runtime import AgentTerminalResult, AgentTool
from app.generation_v2.schemas import SourceTriageCompletion
from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_tool_handlers import SourceTriageWorkspaceToolHandlers
from app.generation_v2.source_triage_workspace_tool_specs import (
    source_triage_finalizer_tools,
    source_triage_planner_tools,
    source_triage_set_tools,
)
from app.models.generation_run import GenerationRun


def _checked_page_output(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded) > MODEL_TOOL_OUTPUT_MAX_CHARS:
        return {
            "accepted": False,
            "error": "triage_workspace_item_exceeds_model_read_contract",
            "max_chars": MODEL_TOOL_OUTPUT_MAX_CHARS,
        }
    return payload


def _page_metadata(*, total: int, cursor: int, returned: int) -> dict[str, Any]:
    next_offset = cursor + returned
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - next_offset),
        "cursor": str(cursor),
        "next_cursor": str(next_offset) if next_offset < total else "",
    }


def _full_source_set_item(source_set: dict[str, Any]) -> dict[str, Any]:
    return {
        "set_id": source_set.get("set_id"),
        "label": str(source_set.get("label") or ""),
        "purpose": str(source_set.get("purpose") or ""),
        "source_refs": source_set.get("source_refs") or [],
        "question_ids": source_set.get("question_ids") or [],
        "question_count": len(source_set.get("question_ids") or []),
        # Completed workspaces from the pre-canonical contract stored full
        # question text on the set. Keep it recoverable without putting it
        # back into compact previews.
        "questions": source_set.get("questions") or [],
        "rationale": str(source_set.get("rationale") or ""),
        "priority": source_set.get("priority"),
        "status": source_set.get("status"),
        "findings_summary": str(source_set.get("findings_summary") or ""),
        "deferred_reason": str(source_set.get("deferred_reason") or ""),
        "evidence_ids": [str(item) for item in source_set.get("evidence_ids") or []],
        "observation_count": len(source_set.get("observations") or []),
        "written_action_count": len(source_set.get("written_actions") or []),
    }


def _full_triage_observation_item(
    observation: dict[str, Any],
    *,
    observation_number: int,
    include_evidence_ids: bool,
) -> dict[str, Any]:
    evidence_ids = [str(item) for item in observation.get("evidence_ids") or [] if str(item).strip()]
    return {
        "observation_number": observation_number,
        "source_refs": observation.get("source_refs") or [],
        **({"evidence_ids": evidence_ids} if include_evidence_ids and evidence_ids else {}),
        "evidence_count": len(evidence_ids),
        "evidence_omitted_count": 0 if include_evidence_ids else len(evidence_ids),
        "note": str(observation.get("note") or ""),
        "pending_questions": [str(item) for item in observation.get("pending_questions") or []],
        "created_at": observation.get("created_at"),
    }


def _full_plan_summary_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    summary = str(workspace.get("plan_summary") or "")
    items = [{"summary": summary}] if cursor == 0 and summary else []
    return {
        "accepted": True,
        "section": "plan_summary",
        "items": items,
        "page": _page_metadata(total=1 if summary else 0, cursor=cursor, returned=len(items)),
    }


def _full_deferred_source_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    deferred = [item for item in workspace.get("deferred_sources") or [] if isinstance(item, dict)]
    page = deferred[cursor : cursor + limit]
    items = [
        {
            "source_ref": item.get("source_ref"),
            "reason": str(item.get("reason") or ""),
        }
        for item in page
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "deferred_sources",
            "items": items,
            "page": _page_metadata(total=len(deferred), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _full_source_map_repair_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    repairs = [item for item in workspace.get("source_map_repair_items") or [] if isinstance(item, dict)]
    page = repairs[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "source_map_repairs",
            "items": page,
            "page": _page_metadata(total=len(repairs), cursor=cursor, returned=len(page)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _full_canonical_item_page(
    workspace: dict[str, Any],
    *,
    section: str,
    cursor: int,
    limit: int = 1,
) -> dict[str, Any]:
    from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash

    field_by_section = {
        "coverage_questions": "coverage_questions",
        "source_assessments": "source_assessments",
        "coverage_outcomes": "coverage_outcomes",
        "coverage_syntheses": "coverage_syntheses",
    }
    field = field_by_section[section]
    values = [item for item in workspace.get(field) or [] if isinstance(item, dict)]
    page = values[cursor : cursor + limit]
    source_ref_by_id = {
        str(item.get("source_id") or ""): str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict) and item.get("source_id") and item.get("source_ref")
    }
    serializers = {
        "coverage_questions": _model_safe_coverage_question,
        "source_assessments": _model_safe_source_assessment,
        "coverage_outcomes": lambda item: _model_safe_coverage_outcome(
            item,
            source_ref_by_id=source_ref_by_id,
        ),
        "coverage_syntheses": lambda item: _model_safe_coverage_synthesis(
            item,
            source_ref_by_id=source_ref_by_id,
            current_revision=int(workspace.get("triage_semantic_revision") or 0),
            current_input_hash=coverage_synthesis_input_hash(
                workspace,
                str(item.get("question_id") or ""),
            ),
        ),
    }
    return fit_model_page(
        {
            "accepted": True,
            "section": section,
            "items": [serializers[section](item) for item in page],
            "page": _page_metadata(total=len(values), cursor=cursor, returned=len(page)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _model_safe_coverage_question(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": item.get("question_id"),
        "question": str(item.get("question") or ""),
        "kind": item.get("kind"),
        "importance": item.get("importance"),
        "rationale": str(item.get("rationale") or ""),
        "parent_question_id": item.get("parent_question_id"),
        "origin": item.get("origin"),
    }


def _model_safe_source_assessment(item: dict[str, Any]) -> dict[str, Any]:
    source_ref = str(item.get("source_ref") or "")
    return {
        "assessment_ref": f"assessment:{source_ref}" if source_ref else "",
        "set_id": item.get("set_id"),
        "source_ref": source_ref,
        "processing_status": item.get("processing_status"),
        "representation_status": item.get("representation_status"),
        "representation_reason": str(item.get("representation_reason") or ""),
        "relevance": item.get("relevance"),
        "relevance_reason": str(item.get("relevance_reason") or ""),
        "authority": item.get("authority"),
        "authority_reason": str(item.get("authority_reason") or ""),
        "uniqueness": item.get("uniqueness"),
        "uniqueness_reason": str(item.get("uniqueness_reason") or ""),
        "disposition": item.get("disposition"),
        "disposition_reason": str(item.get("disposition_reason") or ""),
        "coverage_question_ids": list(item.get("coverage_question_ids") or []),
        "editorial_functions": list(item.get("editorial_functions") or []),
        "evidence_ids": list(item.get("evidence_ids") or []),
        "limitations": list(item.get("limitations") or []),
        "confidence": item.get("confidence"),
        "updated_at": item.get("updated_at"),
    }


def _model_safe_coverage_outcome(
    item: dict[str, Any],
    *,
    source_ref_by_id: dict[str, str],
) -> dict[str, Any]:
    source_refs = [str(value) for value in item.get("source_refs") or [] if str(value).strip()]
    if not source_refs:
        source_refs = [
            source_ref_by_id[str(source_id)]
            for source_id in item.get("source_ids") or []
            if source_ref_by_id.get(str(source_id))
        ]
    source_refs = list(dict.fromkeys(source_refs))
    set_id = str(item.get("set_id") or "")
    question_id = str(item.get("question_id") or "")
    return {
        "outcome_ref": f"outcome:{question_id}:{set_id}" if question_id and set_id else "",
        "set_id": set_id,
        "question_id": question_id,
        "status": item.get("status"),
        "source_refs": source_refs,
        "evidence_ids": list(item.get("evidence_ids") or []),
        "orientation_summary": str(item.get("orientation_summary") or ""),
        "missing_detail": str(item.get("missing_detail") or ""),
        "conflict_summary": str(item.get("conflict_summary") or ""),
        "deferred_reason": str(item.get("deferred_reason") or ""),
        "confidence": item.get("confidence"),
        "next_action": item.get("next_action"),
        "updated_at": item.get("updated_at"),
    }


def _model_safe_coverage_synthesis(
    item: dict[str, Any],
    *,
    source_ref_by_id: dict[str, str],
    current_revision: int,
    current_input_hash: str,
) -> dict[str, Any]:
    payload = _model_safe_coverage_outcome(item, source_ref_by_id=source_ref_by_id)
    payload.pop("set_id", None)
    payload["synthesis_ref"] = str(item.get("outcome_id") or "")
    payload["input_current"] = bool(
        int(item.get("input_revision") or 0) == current_revision
        and str(item.get("input_hash") or "") == current_input_hash
    )
    payload.pop("outcome_ref", None)
    return payload


def _source_set_page_tool(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    current_set_id: str | None = None,
    include_evidence_ids: bool = False,
) -> AgentTool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        section = str(args.get("section") or "sets")
        cursor_text = str(args.get("cursor") or "0")
        cursor = max(0, int(cursor_text) if cursor_text.isdigit() else 0)
        limit = max(1, min(int(args.get("limit") or 3), 3))
        if section == "plan_summary":
            return _checked_page_output(_full_plan_summary_page(current, cursor=cursor))
        if section == "deferred_sources":
            return _full_deferred_source_page(current, cursor=cursor, limit=limit)
        if section == "source_map_repairs":
            return _full_source_map_repair_page(current, cursor=cursor, limit=limit)
        if section in {"coverage_questions", "source_assessments", "coverage_outcomes", "coverage_syntheses"}:
            return _full_canonical_item_page(current, section=section, cursor=cursor, limit=limit)
        if section == "sets":
            payload = _workspace.workspace_summary_for_prompt(current, max_sets=limit, set_cursor=cursor)
            return fit_model_page(
                {
                    "accepted": True,
                    "section": "sets",
                    "source_sets": payload.get("source_sets") or [],
                    "page": payload.get("source_sets_page") or {},
                },
                requested_limit=limit,
                items_key="source_sets",
                error_code="triage_workspace_item_exceeds_model_read_contract",
            )

        requested_set_id = str(
            args.get("set_id") or current_set_id or ((current.get("current_set") or {}).get("set_id")) or ""
        )
        source_set = _workspace._workspace_set(current, requested_set_id) if requested_set_id else None
        if source_set is None:
            return {"accepted": False, "error": "triage_source_set_not_found", "set_id": requested_set_id}
        if section == "current_set":
            items = [_full_source_set_item(source_set)] if cursor == 0 else []
            return _checked_page_output(
                {
                    "accepted": True,
                    "section": section,
                    "set_id": requested_set_id,
                    "items": items,
                    "page": _page_metadata(total=1, cursor=cursor, returned=len(items)),
                }
            )
        if section == "observations":
            observations = [item for item in source_set.get("observations") or [] if isinstance(item, dict)]
            page = observations[cursor : cursor + limit]
            items = [
                _full_triage_observation_item(
                    item,
                    observation_number=cursor + index,
                    include_evidence_ids=include_evidence_ids,
                )
                for index, item in enumerate(page, start=1)
            ]
            return fit_model_page(
                {
                    "accepted": True,
                    "section": section,
                    "set_id": requested_set_id,
                    "items": items,
                    "page": _page_metadata(total=len(observations), cursor=cursor, returned=len(items)),
                },
                requested_limit=limit,
                error_code="triage_workspace_item_exceeds_model_read_contract",
            )
        return {"accepted": False, "error": "unknown_triage_workspace_section", "section": section}

    return AgentTool(
        name="list_triage_source_sets",
        description=(
            "Page compact source-set listings, or read the full validated plan summary, deferred-source "
            "reason, source-map repair diagnostic, source set, or persisted observation after compact "
            "context/checkpoint truncation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 3},
                "section": {
                    "type": "string",
                    "enum": [
                        "sets",
                        "plan_summary",
                        "deferred_sources",
                        "source_map_repairs",
                        "coverage_questions",
                        "source_assessments",
                        "coverage_outcomes",
                        "coverage_syntheses",
                        "current_set",
                        "observations",
                    ],
                },
                "set_id": {"type": "string"},
            },
            "required": ["cursor", "limit"],
        },
        handler=handler,
    )


async def load_source_triage_workspace(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.load_source_triage_workspace(*args, **kwargs)


async def save_source_triage_workspace(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.save_source_triage_workspace(*args, **kwargs)


async def ensure_capsules_for_refs(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.ensure_capsules_for_refs(*args, **kwargs)


def _metadata_only_capsule(*args: Any, **kwargs: Any) -> Any:
    return _workspace._metadata_only_capsule(*args, **kwargs)


def _capsule_page_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspace._capsule_page_payload(*args, **kwargs)


def _dedupe(*args: Any, **kwargs: Any) -> Any:
    return _workspace._dedupe(*args, **kwargs)


def _stable_set_id(*args: Any, **kwargs: Any) -> Any:
    return _workspace._stable_set_id(*args, **kwargs)


def _merge_deferred(*args: Any, **kwargs: Any) -> Any:
    return _workspace._merge_deferred(*args, **kwargs)


def _workspace_set(*args: Any, **kwargs: Any) -> Any:
    return _workspace._workspace_set(*args, **kwargs)


def _replace_workspace_set(*args: Any, **kwargs: Any) -> Any:
    return _workspace._replace_workspace_set(*args, **kwargs)


def _now(*args: Any, **kwargs: Any) -> Any:
    return _workspace._now(*args, **kwargs)


def build_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    inventory: SourceMap,
    evidence_toolset: Any | None = None,
    mode: str,
    current_set_id: str | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = []
    handlers = SourceTriageWorkspaceToolHandlers(
        db=db,
        run=run,
        workspace=workspace,
        inventory=inventory,
        evidence_toolset=evidence_toolset,
        current_set_id=current_set_id,
    )

    async def planner_resume_loader() -> AgentTerminalResult | None:
        current = await load_source_triage_workspace(db, run.id) or workspace
        if current.get("status") != "planned":
            return None
        return AgentTerminalResult(
            accepted=True,
            completion=SourceTriageCompletion(
                ready=True,
                summary=str(current.get("plan_summary") or "Source triage plan already persisted."),
                notes=[],
            ),
        )

    async def set_resume_loader() -> AgentTerminalResult | None:
        current = await load_source_triage_workspace(db, run.id) or workspace
        source_set = _workspace._workspace_set(current, str(current_set_id or ""))
        if source_set is None or source_set.get("status") not in {"mapped", "deferred", "needs_repair"}:
            return None
        deferred_reason = str(source_set.get("deferred_reason") or "").strip()
        return AgentTerminalResult(
            accepted=True,
            completion=SourceTriageCompletion(
                ready=source_set.get("status") in {"mapped", "deferred"},
                summary=str(source_set.get("findings_summary") or "Source triage set already persisted."),
                notes=[deferred_reason] if deferred_reason else [],
            ),
        )

    if mode == "planner":
        tools.append(_source_set_page_tool(db=db, run=run, workspace=workspace))
        tools.extend(
            source_triage_planner_tools(
                handlers.list_source_capsules_tool,
                handlers.upsert_coverage_question_tool,
                handlers.upsert_triage_source_set_tool,
                handlers.finalize_triage_workspace_plan_tool,
                terminal_resume_loader=planner_resume_loader,
            )
        )
    if mode == "set":
        tools.append(
            _source_set_page_tool(
                db=db,
                run=run,
                workspace=workspace,
                current_set_id=current_set_id,
                include_evidence_ids=True,
            )
        )
        tools.extend(
            source_triage_set_tools(
                handlers.record_triage_observation_tool,
                handlers.upsert_source_assessment_tool,
                handlers.upsert_coverage_outcome_tool,
                handlers.finish_triage_source_set_tool,
                terminal_resume_loader=set_resume_loader,
            )
        )
    if mode == "finalizer":
        tools.append(_source_set_page_tool(db=db, run=run, workspace=workspace))
        tools.extend(
            source_triage_finalizer_tools(
                handlers.finish_triage_source_set_tool,
                handlers.upsert_source_assessment_tool,
                handlers.upsert_coverage_outcome_tool,
                handlers.upsert_coverage_synthesis_tool,
            )
        )
    return tools
