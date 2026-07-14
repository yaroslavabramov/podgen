from __future__ import annotations

import json
from typing import Any

from app.generation_v2.runtime_checkpoint import compact_checkpoint_value

CHECKPOINT_TASK_BRIEF_CHARS = 4500
CHECKPOINT_LAST_TOOL_RESULT_LIMIT = 12


def checkpoint_restart_message(
    *,
    prompt: str,
    reason: str,
    previous_chain_id: str,
    next_chain_id: str,
    checkpoint_index: int,
    state: dict[str, Any],
    last_tool_results: list[dict[str, Any]],
) -> dict[str, str]:
    task_brief = checkpoint_task_brief(prompt)
    task_brief_truncated = len(" ".join((prompt or "").split())) > CHECKPOINT_TASK_BRIEF_CHARS
    tool_result_previews = last_tool_results[:CHECKPOINT_LAST_TOOL_RESULT_LIMIT]
    payload = {
        "checkpoint_reason": reason,
        "previous_chain_id": previous_chain_id,
        "next_chain_id": next_chain_id,
        "checkpoint_index": checkpoint_index,
        "task_brief_preview": task_brief,
        "task_brief_chars": len(" ".join((prompt or "").split())),
        "task_brief_truncated": task_brief_truncated,
        "compact_state_from_db": state,
        "last_tool_result_previews": tool_result_previews,
        "last_tool_result_count": len(last_tool_results),
        "last_tool_result_omitted_count": max(0, len(last_tool_results) - len(tool_result_previews)),
        "instructions": checkpoint_restart_instructions(state),
    }
    compact, _ = _runtime_compact_checkpoint_value()(
        payload,
        max_string_chars=1800,
        max_list_items=90,
        max_depth=6,
    )
    return {
        "role": "user",
        "content": "Responses chain checkpoint restart. Continue from compact state:\n"
        + json.dumps(compact, ensure_ascii=False),
    }


def checkpoint_restart_instructions(state: dict[str, Any]) -> str:
    base = (
        "Continue the same agent task from this compact checkpoint. "
        "Do not restart from scratch. Treat DB state and write actions as authoritative. "
    )
    if state.get("phase") == "v2_source_triage":
        return base + (
            "Use compact_state_from_db.corpus.source_inventory as the source list already seen. "
            "Use compact_state_from_db.source_triage_workspace as the durable source-set plan and current-set progress. "
            "Use list_triage_source_sets section=current_set to restore full current-set fields, then page section=observations from cursor 0; page omitted source-set listings with section=sets. "
            "Do not repeat list_sources pages already shown in source_inventory_progress; continue from next_cursor only when all_sources_listed is false. "
            "Source triage workflow: work in small source sets. "
            "If compact_state_from_db.source_triage_progress.read_without_map_write_refs is non-empty, prefer writing the appropriate upsert_source_card, record_source_cluster, record_source_relationship, or record_gap entries before reading more. "
            "For a cluster or relationship, read/compare the relevant 2-5 sources, then write that map entry immediately."
        )
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
                "Continue only the current investigation job. Persist new findings as observations/atoms/risks/gaps, resolve or defer every planned question, then call finish_research_job."
            )
        if agent_name == "research_finalizer_agent":
            return instructions + (
                "Continue memo finalization from persisted atoms, gaps, and risks; page omitted items, merge duplicates when needed, then finalize_research_memo."
            )
        return instructions
    return base


def checkpoint_task_brief(prompt: str) -> str:
    text = " ".join((prompt or "").split())
    if len(text) <= CHECKPOINT_TASK_BRIEF_CHARS:
        return text
    return text[:CHECKPOINT_TASK_BRIEF_CHARS].rstrip() + "..."


def _runtime_compact_checkpoint_value() -> Any:
    from app.generation_v2 import runtime

    return getattr(runtime, "_compact_checkpoint_value", compact_checkpoint_value)


__all__ = [
    "CHECKPOINT_LAST_TOOL_RESULT_LIMIT",
    "CHECKPOINT_TASK_BRIEF_CHARS",
    "checkpoint_restart_instructions",
    "checkpoint_restart_message",
    "checkpoint_task_brief",
]
