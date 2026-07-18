from __future__ import annotations

import json
from typing import Any

from app.generation_v2.runtime_checkpoint import compact_checkpoint_value
from app.generation_v2.runtime_checkpoint_context import validate_checkpoint_resume_context
from app.generation_v2.runtime_contracts import CheckpointResumeContext

CHECKPOINT_LAST_TOOL_RESULT_LIMIT = 12


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
    compact_state, _ = _runtime_compact_checkpoint_value()(
        state,
        max_string_chars=1800,
        max_list_items=90,
        max_depth=6,
    )
    compact_tool_results, _ = _runtime_compact_checkpoint_value()(
        tool_result_previews,
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
                "list_triage_source_sets sections current_set_state, source_read_receipts, reading_notes, "
                "source_assessments, source_findings, coverage_outcomes, and assigned_questions. Continue only from "
                "delivered read ranges. Execute the persisted retry arguments for pending text reads; retry pending "
                "native attachments. Use source reads only for unfinished work and do not reread completed "
                "source analyses. Save useful partial "
                "understanding as a reading note, then save one complete source-analysis revision with "
                "save_source_analysis. Refresh every stale local outcome with upsert_coverage_outcome. Do not write "
                "cards, clusters, quotes, or global gaps. Call finish_triage_source_set only when all source analyses "
                "and assigned or discovered question outcomes are current; status is mapped or deferred only."
            )
        if agent_name == "source_triage_finalizer_agent":
            return instructions + (
                "Review only persisted triage state. Page every source_findings record and every coverage question; "
                "repair missing or unsupported links, then refresh affected local outcomes. Read the complete "
                "coverage_question_packets stream and write one run-wide result per requirement and question. Check "
                "whether another set's findings close each local gap. Only afterward build useful SourceMap entries "
                "and genuine global gaps. Read exact evidence only for a concrete conflict or doubtful causal link, "
                "resolve open source-map repairs, then call finalize_source_map."
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
                "Continue only the current investigation job. Persist new findings as observations/atoms/risks/gaps, resolve or defer every planned question, then call finish_research_job."
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


__all__ = [
    "CHECKPOINT_LAST_TOOL_RESULT_LIMIT",
    "checkpoint_restart_instructions",
    "checkpoint_restart_message",
]
