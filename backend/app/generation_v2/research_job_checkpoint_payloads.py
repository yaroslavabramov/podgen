from __future__ import annotations

import json
from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.research_job_prompt_payloads import compact_research_job_prompt_payload

MAX_CHECKPOINT_LAST_READS = 8
MAX_CHECKPOINT_QUESTION_FINDING_LEADS = 6
MAX_CHECKPOINT_QUESTION_GAP_LEADS = 3
MAX_CHECKPOINT_READ_PREVIEW_CHARS = 450


def compact_research_job_checkpoint_payload(
    workspace: dict[str, Any],
    *,
    current_job_id: str,
    durable_paging_progress: list[dict[str, Any]] | None = None,
    tool_call_counts: dict[str, Any] | None = None,
    last_tool_results: list[dict[str, Any]] | None = None,
    recent_tool_traces: list[Any] | None = None,
) -> dict[str, Any]:
    """Compact mutable state for a research-job checkpoint restart."""

    payload = compact_research_job_prompt_payload(workspace, current_job_id, source_map_summary={})
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    recent_read_previews = _recent_read_previews(
        recent_tool_traces=recent_tool_traces or [],
        last_tool_results=last_tool_results or [],
    )
    return {
        "current_job_detail": {
            "job_id": job.get("job_id"),
            "coverage_question_ids": job.get("coverage_question_ids") or [],
            "questions": job.get("questions") or [],
            "requirements": job.get("requirements") or [],
            "question_level_leads": _checkpoint_question_level_leads(job.get("question_level_leads") or {}),
            "source_scope": job.get("source_scope") or {},
            "required_atom_kinds": job.get("required_atom_kinds") or [],
            "persisted_progress": job.get("persisted_progress") or {},
            "last_targeted_requirement_id": _last_targeted_requirement_id(
                workspace,
                current_job_id,
                recent_tool_traces=recent_tool_traces or [],
                last_tool_results=last_tool_results or [],
            ),
        },
        "durable_paging_progress": durable_paging_progress or {},
        "tool_call_counts": tool_call_counts or {},
        "recent_read_previews": recent_read_previews,
        "checkpoint_usage": (
            "Continue the same current requirement from persisted progress, recent_read_previews, and included leads. "
            "If the active requirement has no explicit leads, use question_level_leads or a targeted pageable lookup."
        ),
    }


def compact_research_checkpoint_root_state(
    state: dict[str, Any],
    *,
    last_tool_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Drop generic corpus/catalog state from research-job restart messages."""

    workspace = state.get("research_workspace")
    if not isinstance(workspace, dict):
        workspace = {}
    checkpoint_workspace = (
        compact_research_job_checkpoint_payload(
            {"investigation_jobs": []},
            current_job_id="",
            durable_paging_progress=_paging_progress_list(state.get("durable_paging_progress")),
            tool_call_counts=state.get("tool_call_counts") if isinstance(state.get("tool_call_counts"), dict) else {},
            last_tool_results=last_tool_results,
        )
        if "current_job_detail" not in workspace
        else _checkpoint_workspace_from_compact_state(
            workspace,
            last_tool_results=last_tool_results or [],
        )
    )
    if isinstance(workspace, dict) and workspace.get("status") and "status" not in checkpoint_workspace:
        checkpoint_workspace["status"] = workspace.get("status")
    return {
        "phase": state.get("phase"),
        "agent_name": state.get("agent_name"),
        "run_intent": state.get("run_intent") or {},
        "research_workspace": checkpoint_workspace,
        "durable_paging_progress": _paging_progress_list(state.get("durable_paging_progress")),
        "tool_call_counts": state.get("tool_call_counts") or {},
    }


def _checkpoint_workspace_from_compact_state(
    workspace: dict[str, Any],
    *,
    last_tool_results: list[dict[str, Any]],
) -> dict[str, Any]:
    existing_recent_reads = [item for item in workspace.get("recent_read_previews") or [] if isinstance(item, dict)]
    current_recent_reads = _recent_read_previews(last_tool_results=last_tool_results)
    payload: dict[str, Any] = {
        "current_job_detail": workspace.get("current_job_detail") or {},
        "recent_read_previews": _dedupe_recent_previews(
            [*existing_recent_reads, *current_recent_reads]
        )[-MAX_CHECKPOINT_LAST_READS:],
    }
    for key in ("durable_paging_progress", "tool_call_counts", "checkpoint_usage"):
        if key in workspace:
            payload[key] = _paging_progress_list(workspace.get(key)) if key == "durable_paging_progress" else workspace.get(key)
    return payload


def _last_targeted_requirement_id(
    workspace: dict[str, Any],
    current_job_id: str,
    *,
    recent_tool_traces: list[Any],
    last_tool_results: list[dict[str, Any]],
) -> str | None:
    valid_requirement_ids = _job_requirement_ids(workspace, current_job_id)
    for event in reversed(
        _normalized_tool_events(recent_tool_traces=recent_tool_traces, last_tool_results=last_tool_results)
    ):
        requirement_id = str(event.get("requirement_id") or "")
        if requirement_id and requirement_id in valid_requirement_ids:
            return requirement_id
    job = _workspace_job(workspace, current_job_id) or {}
    requirement_id = str(job.get("last_targeted_requirement_id") or "")
    if requirement_id and requirement_id in valid_requirement_ids:
        return requirement_id
    return None


def _job_requirement_ids(workspace: dict[str, Any], current_job_id: str) -> set[str]:
    job = _workspace_job(workspace, current_job_id) or {}
    return {
        str(requirement.get("requirement_id") or "")
        for requirement in job.get("coverage_requirements") or []
        if isinstance(requirement, dict) and str(requirement.get("requirement_id") or "")
    }


def _workspace_job(workspace: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    for item in workspace.get("investigation_jobs") or []:
        if isinstance(item, dict) and str(item.get("job_id") or "") == str(job_id):
            return item
    return None


def _recent_read_previews(
    *,
    recent_tool_traces: list[Any] | None = None,
    last_tool_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for result in reversed(
        _normalized_tool_events(
            recent_tool_traces=recent_tool_traces or [],
            last_tool_results=last_tool_results or [],
        )
    ):
        tool_name = str(result.get("tool_name") or "")
        if tool_name not in {
            "read_evidence",
            "read_source_window",
            "read_source_with_model",
            "search_within_source",
            "search_corpus",
            "list_research_workspace_items",
        }:
            continue
        previews.append(
            {
                "tool_name": tool_name,
                "requirement_id": str(result.get("requirement_id") or ""),
                "query": str(result.get("query") or ""),
                "source_ref": str(result.get("source_ref") or ""),
                "evidence_id": str(result.get("evidence_id") or ""),
                **_preview_fields("result", result.get("result") or {}, MAX_CHECKPOINT_READ_PREVIEW_CHARS),
            }
        )
        if len(previews) >= MAX_CHECKPOINT_LAST_READS:
            break
    return list(reversed(previews))


def _normalized_tool_events(
    *,
    recent_tool_traces: list[Any],
    last_tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trace in recent_tool_traces:
        input_json = _dict_payload(getattr(trace, "input_json", None) if not isinstance(trace, dict) else trace.get("input_json") or trace.get("arguments"))
        output_json = _dict_payload(getattr(trace, "output_json", None) if not isinstance(trace, dict) else trace.get("output_json") or trace.get("result") or trace.get("output"))
        tool_name = str(getattr(trace, "tool_name", "") if not isinstance(trace, dict) else trace.get("tool_name") or trace.get("name") or "")
        if _accepted_output(output_json):
            event = _normalized_tool_event(tool_name, input_json, output_json)
            fingerprint = _event_fingerprint(event)
            if fingerprint not in seen:
                seen.add(fingerprint)
                events.append(event)
    for result in last_tool_results:
        if not isinstance(result, dict):
            continue
        input_json = _dict_payload(result.get("input_json") or result.get("arguments") or result.get("input"))
        output_json = _dict_payload(result.get("output_json") or result.get("result") or result.get("output") or result)
        tool_name = str(result.get("tool_name") or result.get("name") or "")
        if _accepted_output(output_json):
            event = _normalized_tool_event(tool_name, input_json, output_json)
            fingerprint = _event_fingerprint(event)
            if fingerprint not in seen:
                seen.add(fingerprint)
                events.append(event)
    return [event for event in events if event.get("tool_name")]


def _checkpoint_question_level_leads(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "finding_leads": [
            dict(item)
            for item in value.get("finding_leads") or []
            if isinstance(item, dict)
        ][:MAX_CHECKPOINT_QUESTION_FINDING_LEADS],
        "gap_leads": [
            dict(item)
            for item in value.get("gap_leads") or []
            if isinstance(item, dict)
        ][:MAX_CHECKPOINT_QUESTION_GAP_LEADS],
        "usage": value.get("usage") or "",
    }


def _normalized_tool_event(tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    filter_payload = result.get("filter") if isinstance(result.get("filter"), dict) else {}
    evidence_payload = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    return {
        "tool_name": tool_name,
        "requirement_id": _first_string(arguments, "requirement_id")
        or _first_string(filter_payload, "requirement_id")
        or _first_string(result, "requirement_id"),
        "query": _first_string(arguments, "query") or _first_string(filter_payload, "query"),
        "source_ref": _first_string(arguments, "source_ref")
        or _first_string(result, "source_ref", "source")
        or _first_string(evidence_payload, "source_ref"),
        "evidence_id": _first_string(arguments, "evidence_id")
        or _first_string(result, "evidence_id", "selected_evidence_id")
        or _first_string(evidence_payload, "evidence_id"),
        "cursor": _first_string(arguments, "cursor") or _first_string(result.get("page"), "cursor"),
        "result": result,
    }


def _event_fingerprint(event: dict[str, Any]) -> str:
    payload = {
        "tool_name": event.get("tool_name") or "",
        "requirement_id": event.get("requirement_id") or "",
        "query": event.get("query") or "",
        "source_ref": event.get("source_ref") or "",
        "evidence_id": event.get("evidence_id") or "",
        "cursor": event.get("cursor") or "",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _accepted_output(output_json: dict[str, Any]) -> bool:
    if output_json.get("accepted") is False or output_json.get("found") is False:
        return False
    if output_json.get("error") and output_json.get("accepted") is not True:
        return False
    return True


def _dict_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _paging_progress_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _dedupe_recent_previews(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in values:
        fingerprint = json.dumps(
            {
                "tool_name": item.get("tool_name") or "",
                "requirement_id": item.get("requirement_id") or "",
                "query": item.get("query") or "",
                "source_ref": item.get("source_ref") or "",
                "evidence_id": item.get("evidence_id") or "",
                "result_preview": item.get("result_preview") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(item)
    return deduped


def _first_string(payload: Any, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = model_preview_text(_json_safe(value), collapse_whitespace=True)
    preview = text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"
    return {
        f"{name}_preview": preview,
        f"{name}_chars": len(text),
        f"{name}_truncated": len(text) > limit,
    }


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


__all__ = [
    "compact_research_checkpoint_root_state",
    "compact_research_job_checkpoint_payload",
]
