from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.debug_dump import dump_v2_debug
from app.generation_v2.runtime_contracts import CheckpointResumeContext
from app.generation_v2.runtime_checkpoint_state import (
    checkpoint_reason as _checkpoint_reason,
    checkpoint_restart as _checkpoint_restart,
)
from app.generation_v2.runtime_debug import runtime_debug_payload as _runtime_debug_payload


@dataclass
class HostedFollowupAdvance:
    checkpoint_index: int
    chain_id: str
    input_payload: Any
    previous_response_id: str | None
    chain_round: int
    chain_model_tool_output_chars: int


async def advance_hosted_followup_without_function_calls(
    *,
    db: AsyncSession,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    round_idx: int,
    prompt_hash: str,
    system: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    tools: list[dict[str, Any]],
    policy: Any,
    input_payload: Any,
    response: Any,
    usage: dict[str, Any],
    file_search_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    function_calls: list[dict[str, Any]],
    hosted_followup: dict[str, Any],
    checkpoint: Any,
    checkpoint_resume_context: CheckpointResumeContext | None,
    checkpoint_index: int,
    chain_id: str,
    chain_round: int,
    chain_model_tool_output_chars: int,
) -> HostedFollowupAdvance:
    hosted_chars = len(json.dumps(hosted_followup["message"], ensure_ascii=False))
    chain_model_tool_output_chars += hosted_chars
    checkpoint_reason = _checkpoint_reason(
        checkpoint,
        chain_round=chain_round,
        usage=usage,
        chain_model_tool_output_chars=chain_model_tool_output_chars,
    )
    dump_v2_debug(
        run_id=str(generation_run_id),
        phase=phase,
        agent_name=agent_name,
        dump_type=f"round-{round_idx + 1:02d}-hosted-followup",
        payload=_runtime_debug_payload(
            generation_run_id=generation_run_id,
            phase=phase,
            agent_name=agent_name,
            round_idx=round_idx,
            prompt_hash=prompt_hash,
            system=system,
            prompt=prompt,
            schema_name=schema_name,
            schema=schema,
            tools=tools,
            policy=policy,
            input_payload=input_payload,
            response=response,
            usage=usage,
            file_search_calls=file_search_calls,
            web_search_calls=web_search_calls,
            function_calls=function_calls,
            hosted_followup=hosted_followup,
        ),
    )
    if checkpoint_reason:
        checkpoint_index, chain_id, input_payload = await _checkpoint_restart(
            db=db,
            generation_run_id=generation_run_id,
            phase=phase,
            agent_name=agent_name,
            resume_context=checkpoint_resume_context,
            previous_chain_id=chain_id,
            next_checkpoint_index=checkpoint_index + 1,
            reason=checkpoint_reason,
            last_tool_results=[
                {
                    "tool_name": "hosted_file_search",
                    "result": hosted_followup["message"].get("content"),
                }
            ],
            prompt_hash=prompt_hash,
        )
        return HostedFollowupAdvance(
            checkpoint_index=checkpoint_index,
            chain_id=chain_id,
            input_payload=input_payload,
            previous_response_id=None,
            chain_round=0,
            chain_model_tool_output_chars=0,
        )
    return HostedFollowupAdvance(
        checkpoint_index=checkpoint_index,
        chain_id=chain_id,
        input_payload=[hosted_followup["message"]],
        previous_response_id=response.id,
        chain_round=chain_round,
        chain_model_tool_output_chars=chain_model_tool_output_chars,
    )


def append_hosted_followup_tool_output(
    *,
    hosted_followup: dict[str, Any],
    tool_outputs: list[dict[str, Any]],
    checkpoint_tool_results: list[dict[str, Any]],
    chain_model_tool_output_chars: int,
) -> int:
    hosted_chars = len(json.dumps(hosted_followup["message"], ensure_ascii=False))
    chain_model_tool_output_chars += hosted_chars
    tool_outputs.append(hosted_followup["message"])
    checkpoint_tool_results.append(
        {
            "tool_name": "hosted_file_search",
            "result": hosted_followup["message"].get("content"),
        }
    )
    return chain_model_tool_output_chars


__all__ = ["HostedFollowupAdvance", "advance_hosted_followup_without_function_calls", "append_hosted_followup_tool_output"]
