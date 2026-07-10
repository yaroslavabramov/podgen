from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.debug_dump import dump_v2_debug
from app.generation_v2.runtime_checkpoint_state import (
    commit_runtime_side_effects as _commit_runtime_side_effects,
    compact_checkpoint_arguments as _compact_checkpoint_arguments,
    model_output_stats as _model_output_stats,
    trace_output_with_runtime as _trace_output_with_runtime,
)
from app.generation_v2.runtime_contracts import AgentTool
from app.generation_v2.runtime_debug import safe_tool_dump_name as _safe_tool_dump_name
from app.generation_v2.runtime_tool_output import ToolOutputContractViolation, model_facing_tool_output as _model_facing_tool_output


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeToolCallResult:
    tool_outputs: list[dict[str, Any]]
    checkpoint_tool_results: list[dict[str, Any]]
    chain_model_tool_output_chars: int


async def process_runtime_function_calls(
    db: AsyncSession,
    *,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    round_number: int,
    chain_id: str,
    checkpoint_index: int,
    chain_round: int,
    function_calls: list[dict[str, Any]],
    local_tools: list[AgentTool] | None,
    chain_model_tool_output_chars: int,
) -> RuntimeToolCallResult:
    local_tool_map = {tool.name: tool for tool in local_tools or []}
    tool_outputs: list[dict[str, Any]] = []
    checkpoint_tool_results: list[dict[str, Any]] = []
    for call_index, call in enumerate(function_calls, start=1):
        debug_base = {
            "run_id": str(generation_run_id),
            "phase": phase,
            "agent_name": agent_name,
            "round": round_number,
            "chain_id": chain_id,
            "checkpoint_index": checkpoint_index,
            "chain_round": chain_round,
            "tool_index": call_index,
            "tool_name": call["name"],
            "call_id": call["call_id"],
            "arguments": call["arguments"],
        }
        dump_v2_debug(
            run_id=str(generation_run_id),
            phase=phase,
            agent_name=agent_name,
            dump_type=f"round-{round_number:02d}-tool-{call_index:02d}-{_safe_tool_dump_name(call['name'])}-call",
            payload=debug_base,
        )
        tool = local_tool_map.get(call["name"])
        if tool is None:
            output = {"error": f"Unknown local tool: {call['name']}"}
        else:
            try:
                output = await tool.handler(call["arguments"])
            except Exception as exc:
                logger.warning("V2 local tool failed: %s", call["name"], exc_info=True)
                output = {"error": str(exc)}
        raw_output_chars = len(json.dumps(output, ensure_ascii=False, default=str))
        dump_v2_debug(
            run_id=str(generation_run_id),
            phase=phase,
            agent_name=agent_name,
            dump_type=f"round-{round_number:02d}-tool-{call_index:02d}-{_safe_tool_dump_name(call['name'])}-raw-output",
            payload={
                **debug_base,
                "raw_output_chars": raw_output_chars,
                "raw_output": output,
            },
        )
        try:
            model_output = _model_facing_tool_output(call["name"], output)
            output_stats = _model_output_stats(call["name"], model_output)
        except ToolOutputContractViolation as exc:
            output_stats = _model_output_stats(call["name"], output, contract_violation=str(exc))
            dump_v2_debug(
                run_id=str(generation_run_id),
                phase=phase,
                agent_name=agent_name,
                dump_type=f"round-{round_number:02d}-tool-{call_index:02d}-{_safe_tool_dump_name(call['name'])}-contract-violation",
                payload={
                    **debug_base,
                    "contract_violation": str(exc),
                    "model_output_stats": output_stats,
                    "raw_output_chars": raw_output_chars,
                    "raw_output": output,
                },
            )
            await save_tool_trace(
                db,
                generation_run_id=generation_run_id,
                phase=phase,
                agent_name=agent_name,
                tool_name=call["name"],
                input_json=call["arguments"],
                output_json=_trace_output_with_runtime(
                    output,
                    output_stats,
                    chain_id=chain_id,
                    checkpoint_index=checkpoint_index,
                    chain_round=chain_round,
                ),
                provider_call_id=call["call_id"],
            )
            await _commit_runtime_side_effects(db)
            raise
        chain_model_tool_output_chars += output_stats["chars"]
        await save_tool_trace(
            db,
            generation_run_id=generation_run_id,
            phase=phase,
            agent_name=agent_name,
            tool_name=call["name"],
            input_json=call["arguments"],
            output_json=_trace_output_with_runtime(
                output,
                output_stats,
                chain_id=chain_id,
                checkpoint_index=checkpoint_index,
                chain_round=chain_round,
            ),
            provider_call_id=call["call_id"],
        )
        await _commit_runtime_side_effects(db)
        tool_outputs.append(
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": json.dumps(model_output, ensure_ascii=False),
            }
        )
        checkpoint_tool_results.append(
            {
                "tool_name": call["name"],
                "arguments": _compact_checkpoint_arguments(call["arguments"]),
                "result": model_output,
            }
        )
    return RuntimeToolCallResult(
        tool_outputs=tool_outputs,
        checkpoint_tool_results=checkpoint_tool_results,
        chain_model_tool_output_chars=chain_model_tool_output_chars,
    )
