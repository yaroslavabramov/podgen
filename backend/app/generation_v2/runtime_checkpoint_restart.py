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
    payload = {
        "checkpoint_reason": reason,
        "previous_chain_id": previous_chain_id,
        "next_chain_id": next_chain_id,
        "checkpoint_index": checkpoint_index,
        "task_brief": checkpoint_task_brief(prompt),
        "compact_state_from_db": state,
        "last_tool_results": last_tool_results[:CHECKPOINT_LAST_TOOL_RESULT_LIMIT],
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
    instructions = (
        "Continue the same agent task from this compact checkpoint. "
        "Do not restart from scratch. Treat DB state and write actions as authoritative. "
        "Use compact_state_from_db.corpus.source_inventory as the source list you already saw. "
        "Do not repeat list_sources pages already shown in source_inventory_progress; continue from next_cursor only when all_sources_listed is false. "
        "Proceed to source-map write actions when enough evidence/source coverage exists."
    )
    if state.get("phase") == "v2_source_triage":
        instructions += (
            " Source triage workflow: work in small source sets. "
            "If compact_state_from_db.source_triage_progress.read_without_map_write_refs is non-empty, prefer writing the appropriate upsert_source_card, record_source_cluster, record_source_relationship, or record_gap entries before reading more. "
            "For a cluster or relationship, read/compare the relevant 2-5 sources, then write that map entry immediately."
        )
    return instructions


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
