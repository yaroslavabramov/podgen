from __future__ import annotations

import json
from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.runtime_checkpoint import compact_checkpoint_value
from app.generation_v2.runtime_checkpoint_context import validate_checkpoint_resume_context
from app.generation_v2.runtime_contracts import CheckpointResumeContext

CHECKPOINT_LAST_TOOL_RESULT_LIMIT = 12
_EMBEDDED_READ_RESULT_PREVIEW_CHARS = 450
_EMBEDDED_READ_TOOL_NAMES = {
    "read_evidence",
    "read_source_window",
    "read_source_with_model",
    "search_within_source",
    "search_corpus",
    "list_research_workspace_items",
}


def checkpoint_restart_message(
    *,
    resume_context: CheckpointResumeContext,
    reason: str,
    previous_chain_id: str,
    next_chain_id: str,
    checkpoint_index: int,
    state: dict[str, Any],
    last_tool_results: list[dict[str, Any]],
) -> dict[str, str]:
    validate_checkpoint_resume_context(resume_context)
    tool_result_previews = last_tool_results[:CHECKPOINT_LAST_TOOL_RESULT_LIMIT]
    state_for_prompt = _state_for_checkpoint_prompt(state, last_tool_results=tool_result_previews)
    generic_tool_result_previews = _generic_tool_result_previews(
        state,
        state_for_prompt=state_for_prompt,
        tool_result_previews=tool_result_previews,
    )
    compact_state, _ = _runtime_compact_checkpoint_value()(
        state_for_prompt,
        max_string_chars=1800,
        max_list_items=90,
        max_depth=6,
    )
    compact_tool_results, _ = _runtime_compact_checkpoint_value()(
        generic_tool_result_previews,
        max_string_chars=1800,
        max_list_items=CHECKPOINT_LAST_TOOL_RESULT_LIMIT,
        max_depth=5,
    )
    payload = {
        "resume_contract_version": resume_context.contract_version,
        "checkpoint_reason": reason,
        "previous_chain_id": previous_chain_id,
        "next_chain_id": next_chain_id,
        "checkpoint_index": checkpoint_index,
        "workflow_contract": resume_context.workflow_contract,
        "stable_task_context": resume_context.task_context,
        "compact_state_from_db": compact_state,
        "last_tool_result_previews": compact_tool_results,
        "last_tool_result_count": len(last_tool_results),
        "last_tool_result_omitted_count": max(0, len(last_tool_results) - len(tool_result_previews)),
        "instructions": checkpoint_restart_instructions(state),
    }
    return {
        "role": "user",
        "content": "Responses chain checkpoint restart. Continue from compact state:\n"
        + json.dumps(payload, ensure_ascii=False),
    }


def checkpoint_restart_instructions(state: dict[str, Any]) -> str:
    base = (
        "Continue the same agent task from this compact checkpoint. "
        "Do not restart from scratch. Treat DB state and write actions as authoritative. "
        "The workflow_contract and stable_task_context are immutable; compact_state_from_db is the only "
        "authoritative mutable workflow state. Last tool result previews are non-authoritative reading aids. "
        "Continue semantic work in the requested language recorded in compact_state_from_db.run_intent "
        "(or the durable workspace intent); do not switch languages. "
        "For any paginated read, use compact_state_from_db.durable_paging_progress: do not reread completed pages, "
        "and resume an incomplete page sequence from its next_cursor instead of cursor 0. "
    )
    if state.get("phase") == "v2_source_triage":
        agent_name = str(state.get("agent_name") or "")
        instructions = base + (
            "Use compact_state_from_db.corpus.source_inventory as the source list already seen. "
            "Use compact_state_from_db.source_triage_workspace as the durable source-set plan and progress. "
        )
        if agent_name == "source_triage_planner_agent":
            inventory_page = ((state.get("corpus") or {}).get("source_inventory_page") or {})
            inventory_instruction = (
                "The compact source inventory is the complete catalog; do not restart full-corpus capsule paging. "
                if inventory_page.get("complete") is True
                else (
                    "The compact source inventory is only the first durable catalog page. Continue catalog reading "
                    f"with list_source_capsules from cursor {inventory_page.get('next_cursor')!r}; do not reread "
                    "the inventory entries already present in the checkpoint. "
                )
            )
            return instructions + inventory_instruction + (
                "compact_state_from_db.source_triage_workspace.planner_resume lists every accepted coverage question, "
                "accepted source-set assignment, concrete violation, and the backend-computed next_action. "
                "Never recreate, translate, or paraphrase an accepted question or source set as a new write. "
                "Execute that next_action, inspecting only a specifically ambiguous source_ref if it is necessary; "
                "if it still cannot be assigned honestly, use route_sources_for_initial_reading instead of inventing "
                "a procedural coverage question. "
                "then call finalize_triage_workspace_plan when ready."
            )
        if agent_name.startswith("source_triage_set_agent_"):
            return instructions + (
                "Continue only the current source set. Restore complete durable memory through "
                "read_triage_set_state sections set_overview, source_memory, question_index, coverage_hints, "
                "finding_list, and unfinished_work. "
                "Continue only from "
                "delivered read ranges. Execute the persisted retry arguments for pending text reads; retry pending "
                "native attachments. Use source reads only for unfinished work and do not reread completed "
                "source analyses. Save useful partial "
                "understanding as a reading note, then save the completed source analysis with "
                "save_source_analysis. Save source-specific coverage hints when a source materially informs a "
                "question. Do not answer questions and do not write "
                "cards, clusters, quotes, or global gaps. Call finish_triage_source_set only when all source analyses "
                "are current; status is mapped or deferred only."
            )
        if agent_name == "source_triage_finalizer_agent":
            return instructions + (
                "Review only persisted triage state. Page every source_findings record and every coverage question; "
                "repair missing or unsupported source-mapping links and relations. Do not answer questions, refresh "
                "local outcomes, or write run-wide results. Build only useful SourceMap entries and source-map gaps. "
                "Read exact evidence only for a concrete doubtful source-mapping link or relation, resolve open "
                "source-map repairs, then call finalize_source_map."
            )
        return instructions + "Continue only with tools exposed to this agent role."
    if state.get("phase") == "v2_editorial_research":
        agent_name = str(state.get("agent_name") or "")
        instructions = base + (
            "Use compact_state_from_db.research_workspace as the durable research state. "
            "Do not redo mapped/deferred jobs or already persisted observations/atoms. "
        )
        if agent_name == "research_planner_agent":
            return instructions + (
                "Continue the research plan from persisted jobs; page omitted workspace or SourceMap items, add only missing jobs, then finalize_research_plan."
            )
        if agent_name.startswith("research_job_agent_"):
            return instructions + (
                "Continue only the current investigation job and current requirement. Use "
                "compact_state_from_db.research_workspace.current_job_detail.requirements, persisted_progress, "
                "question_level_leads, recent_read_previews, and durable_paging_progress. Do not inspect other jobs "
                "or reread the source catalog. Page broader SourceMap context only for a concrete missing link that "
                "the included leads and targeted job lookups cannot resolve. Save an atom when the active requirement "
                "is supported; save a structured gap and defer only that requirement when it remains unsupported. "
                "Then move to the next requirement and finish only through finish_research_job."
            )
        if agent_name == "research_finalizer_agent":
            return instructions + (
                "Continue memo finalization from persisted atoms, gaps, and risks; page omitted items, merge duplicates when needed, then finalize_research_memo."
            )
        return instructions
    return base


def _runtime_compact_checkpoint_value() -> Any:
    from app.generation_v2 import runtime

    return getattr(runtime, "_compact_checkpoint_value", compact_checkpoint_value)


def _state_for_checkpoint_prompt(
    state: dict[str, Any],
    *,
    last_tool_results: list[dict[str, Any]],
) -> dict[str, Any]:
    if state.get("phase") == "v2_editorial_research" and str(state.get("agent_name") or "").startswith(
        "research_job_agent_"
    ):
        from app.generation_v2.research_job_checkpoint_payloads import compact_research_checkpoint_root_state

        return compact_research_checkpoint_root_state(state, last_tool_results=last_tool_results)
    return state


def _generic_tool_result_previews(
    state: dict[str, Any],
    *,
    state_for_prompt: dict[str, Any],
    tool_result_previews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not _research_job_checkpoint(state):
        return tool_result_previews
    embedded_read_keys = _embedded_recent_read_keys(state_for_prompt)
    if not embedded_read_keys:
        return tool_result_previews
    return [
        result
        for result in tool_result_previews
        if not _is_embedded_accepted_read_result(result, embedded_read_keys)
    ]


def _research_job_checkpoint(state: dict[str, Any]) -> bool:
    return state.get("phase") == "v2_editorial_research" and str(state.get("agent_name") or "").startswith(
        "research_job_agent_"
    )


def _embedded_recent_read_keys(state_for_prompt: dict[str, Any]) -> set[str]:
    workspace = state_for_prompt.get("research_workspace")
    if not isinstance(workspace, dict):
        return set()
    keys: set[str] = set()
    for item in workspace.get("recent_read_previews") or []:
        if not isinstance(item, dict):
            continue
        keys.add(
            _read_result_key(
                tool_name=str(item.get("tool_name") or ""),
                requirement_id=str(item.get("requirement_id") or ""),
                query=str(item.get("query") or ""),
                source_ref=str(item.get("source_ref") or ""),
                evidence_id=str(item.get("evidence_id") or ""),
                result_preview=str(item.get("result_preview") or ""),
            )
        )
    return keys


def _is_embedded_accepted_read_result(result: dict[str, Any], embedded_read_keys: set[str]) -> bool:
    tool_name = str(result.get("tool_name") or result.get("name") or "")
    if tool_name not in _EMBEDDED_READ_TOOL_NAMES:
        return False
    arguments = _dict_payload(result.get("input_json") or result.get("arguments") or result.get("input"))
    output_json = _dict_payload(result.get("output_json") or result.get("result") or result.get("output") or result)
    if not _accepted_tool_output(output_json):
        return False
    filter_payload = output_json.get("filter") if isinstance(output_json.get("filter"), dict) else {}
    evidence_payload = output_json.get("evidence") if isinstance(output_json.get("evidence"), dict) else {}
    key = _read_result_key(
        tool_name=tool_name,
        requirement_id=_first_string(arguments, "requirement_id")
        or _first_string(filter_payload, "requirement_id")
        or _first_string(output_json, "requirement_id"),
        query=_first_string(arguments, "query") or _first_string(filter_payload, "query"),
        source_ref=_first_string(arguments, "source_ref")
        or _first_string(output_json, "source_ref", "source")
        or _first_string(evidence_payload, "source_ref"),
        evidence_id=_first_string(arguments, "evidence_id")
        or _first_string(output_json, "evidence_id", "selected_evidence_id")
        or _first_string(evidence_payload, "evidence_id"),
        result_preview=_tool_result_preview(output_json),
    )
    return key in embedded_read_keys


def _accepted_tool_output(output_json: dict[str, Any]) -> bool:
    if output_json.get("accepted") is False or output_json.get("found") is False:
        return False
    if output_json.get("error") and output_json.get("accepted") is not True:
        return False
    return True


def _read_result_key(
    *,
    tool_name: str,
    requirement_id: str,
    query: str,
    source_ref: str,
    evidence_id: str,
    result_preview: str,
) -> str:
    return json.dumps(
        {
            "tool_name": tool_name,
            "requirement_id": requirement_id,
            "query": query,
            "source_ref": source_ref,
            "evidence_id": evidence_id,
            "result_preview": result_preview,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _tool_result_preview(output_json: dict[str, Any]) -> str:
    text = model_preview_text(_json_safe(output_json), collapse_whitespace=True)
    if len(text) <= _EMBEDDED_READ_RESULT_PREVIEW_CHARS:
        return text
    return text[: max(0, _EMBEDDED_READ_RESULT_PREVIEW_CHARS - 1)].rstrip() + "…"


def _dict_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _first_string(payload: Any, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


__all__ = [
    "CHECKPOINT_LAST_TOOL_RESULT_LIMIT",
    "checkpoint_restart_instructions",
    "checkpoint_restart_message",
]
