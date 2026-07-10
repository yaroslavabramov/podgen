from __future__ import annotations

from app.generation_v2.contracts import V2ToolName
import inspect
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.runtime_checkpoint import compact_checkpoint_value
from app.generation_v2.runtime_checkpoint_restart import (
    CHECKPOINT_LAST_TOOL_RESULT_LIMIT,
    CHECKPOINT_TASK_BRIEF_CHARS,
    checkpoint_restart_instructions,
    checkpoint_restart_message,
    checkpoint_task_brief,
)
from app.generation_v2.runtime_checkpoint_snapshot import build_agent_checkpoint_state
from app.generation_v2.runtime_checkpoint_trace import (
    checkpoint_source_inventory,
    checkpoint_trim,
    compact_checkpoint_arguments,
    model_output_stats,
    trace_output_with_runtime,
)
from app.generation_v2.runtime_checkpoint_progress import (
    corpus_inventory_state,
    source_inventory_progress,
    source_triage_progress,
)
from app.generation_v2.runtime_tool_output import MODEL_FACING_CONTRACT_VERSION, tool_model_facing_schema

CHECKPOINT_STATE_ID_LIMIT = 40

_MODEL_FACING_CONTRACT_VERSION = MODEL_FACING_CONTRACT_VERSION
_compact_checkpoint_value = compact_checkpoint_value
_tool_model_facing_schema = tool_model_facing_schema

def _runtime_compact_checkpoint_value() -> Any:
    return compact_checkpoint_value


def _runtime_tool_model_facing_schema() -> Any:
    return tool_model_facing_schema


def chain_id(phase: str, agent_name: str, prompt_hash: str, checkpoint_index: int) -> str:
    return f"{phase}:{agent_name}:{prompt_hash[:12]}:{checkpoint_index}"


def checkpoint_reason(
    policy: Any,
    *,
    chain_round: int,
    usage: dict[str, Any],
    chain_model_tool_output_chars: int,
) -> str | None:
    if not policy.enabled:
        return None
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    if input_tokens >= policy.max_chain_input_tokens:
        return f"input_tokens>={policy.max_chain_input_tokens}"
    if chain_round >= policy.max_chain_rounds:
        return f"chain_rounds>={policy.max_chain_rounds}"
    if chain_model_tool_output_chars >= policy.max_chain_model_tool_output_chars:
        return f"model_tool_output_chars>={policy.max_chain_model_tool_output_chars}"
    return None


async def checkpoint_restart(
    *,
    db: AsyncSession,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    prompt: str,
    previous_chain_id: str,
    next_checkpoint_index: int,
    reason: str,
    last_tool_results: list[dict[str, Any]],
    prompt_hash: str,
) -> tuple[int, str, list[dict[str, Any]]]:
    state = await build_agent_checkpoint_state(
        db,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
    )
    next_chain_id = chain_id(phase, agent_name, prompt_hash, next_checkpoint_index)
    message = checkpoint_restart_message(
        prompt=prompt,
        reason=reason,
        previous_chain_id=previous_chain_id,
        next_chain_id=next_chain_id,
        checkpoint_index=next_checkpoint_index,
        state=state,
        last_tool_results=last_tool_results,
    )
    await save_tool_trace(
        db,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
        tool_name=V2ToolName.RUNTIME_CHECKPOINT_RESTART,
        input_json={
            "reason": reason,
            "previous_chain_id": previous_chain_id,
            "next_chain_id": next_chain_id,
            "checkpoint_index": next_checkpoint_index,
            "last_tool_result_count": len(last_tool_results),
        },
        output_json={
            "checkpoint_index": next_checkpoint_index,
            "chain_id": next_chain_id,
            "state": state,
            "message_chars": len(message["content"]),
        },
    )
    await commit_runtime_side_effects(db)
    return next_checkpoint_index, next_chain_id, [message]


async def commit_runtime_side_effects(db: AsyncSession) -> None:
    """Persist tool traces and tool-created evidence/actions before the next model call."""
    commit = getattr(db, "commit", None)
    if not callable(commit):
        return
    result = commit()
    if inspect.isawaitable(result):
        await result

__all__ = [
    "checkpoint_source_inventory",
    "checkpoint_trim",
    "compact_checkpoint_arguments",
    "model_output_stats",
    "trace_output_with_runtime",
    "corpus_inventory_state",
    "source_inventory_progress",
    "source_triage_progress",
    "build_agent_checkpoint_state",
    "CHECKPOINT_LAST_TOOL_RESULT_LIMIT",
    "CHECKPOINT_TASK_BRIEF_CHARS",
    "checkpoint_restart_instructions",
    "checkpoint_restart_message",
    "checkpoint_task_brief",
]
