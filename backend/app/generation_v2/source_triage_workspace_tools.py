from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as _workspace
from app.generation_v2.runtime import AgentTool
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
        "rationale": str(source_set.get("rationale") or ""),
        "questions": [str(item) for item in source_set.get("questions") or []],
        "priority": source_set.get("priority"),
        "status": source_set.get("status"),
        "findings_summary": str(source_set.get("findings_summary") or ""),
        "deferred_reason": str(source_set.get("deferred_reason") or ""),
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
        **({"evidence_ids": evidence_ids[:8]} if include_evidence_ids and evidence_ids else {}),
        "evidence_count": len(evidence_ids),
        "evidence_omitted_count": max(0, len(evidence_ids) - 8) if include_evidence_ids else len(evidence_ids),
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


def _full_deferred_source_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    deferred = [item for item in workspace.get("deferred_sources") or [] if isinstance(item, dict)]
    page = deferred[cursor : cursor + 1]
    items = [
        {
            "source_ref": item.get("source_ref"),
            "reason": str(item.get("reason") or ""),
        }
        for item in page
    ]
    return {
        "accepted": True,
        "section": "deferred_sources",
        "items": items,
        "page": _page_metadata(total=len(deferred), cursor=cursor, returned=len(items)),
    }


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
        if section == "plan_summary":
            return _checked_page_output(_full_plan_summary_page(current, cursor=cursor))
        if section == "deferred_sources":
            return _checked_page_output(_full_deferred_source_page(current, cursor=cursor))
        if section == "sets":
            limit = max(1, min(int(args.get("limit") or 3), 3))
            payload = _workspace.workspace_summary_for_prompt(current, max_sets=limit, set_cursor=cursor)
            return _checked_page_output(
                {
                    "accepted": True,
                    "section": "sets",
                    "source_sets": payload.get("source_sets") or [],
                    "page": payload.get("source_sets_page") or {},
                }
            )

        requested_set_id = str(
            args.get("set_id")
            or current_set_id
            or ((current.get("current_set") or {}).get("set_id"))
            or ""
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
            page = observations[cursor : cursor + 1]
            items = [
                _full_triage_observation_item(
                    item,
                    observation_number=cursor + 1,
                    include_evidence_ids=include_evidence_ids,
                )
                for item in page
            ]
            return _checked_page_output(
                {
                    "accepted": True,
                    "section": section,
                    "set_id": requested_set_id,
                    "items": items,
                    "page": _page_metadata(total=len(observations), cursor=cursor, returned=len(items)),
                }
            )
        return {"accepted": False, "error": "unknown_triage_workspace_section", "section": section}

    return AgentTool(
        name="list_triage_source_sets",
        description=(
            "Page compact source-set listings, or read the full validated plan summary, deferred-source "
            "reason, source set, or persisted observation after compact context/checkpoint truncation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 3},
                "section": {
                    "type": "string",
                    "enum": ["sets", "plan_summary", "deferred_sources", "current_set", "observations"],
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

    if mode == "planner":
        tools.append(_source_set_page_tool(db=db, run=run, workspace=workspace))
        tools.extend(
            source_triage_planner_tools(
                handlers.list_source_capsules_tool,
                handlers.upsert_triage_source_set_tool,
                handlers.finalize_triage_workspace_plan_tool,
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
        tools.extend(source_triage_set_tools(handlers.record_triage_observation_tool, handlers.finish_triage_source_set_tool))
    if mode == "finalizer":
        tools.append(_source_set_page_tool(db=db, run=run, workspace=workspace))
        tools.extend(source_triage_finalizer_tools(handlers.finish_triage_source_set_tool))
    return tools
