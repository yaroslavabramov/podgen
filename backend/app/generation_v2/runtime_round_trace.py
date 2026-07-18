from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.debug_dump import dump_v2_debug
from app.generation_v2.runtime_debug import runtime_debug_payload as _runtime_debug_payload
from app.generation_v2.runtime_response_trace import (
    RuntimeResponseTrace,
    runtime_response_trace_payload as _runtime_response_trace_payload,
    save_response_create_trace as _save_response_create_trace,
    set_response_run_outputs as _set_response_run_outputs,
)


async def record_round_response_trace(
    db: AsyncSession,
    *,
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
    run_tree: Any,
    requested_model: str,
    latency_ms: float,
    file_search_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    function_calls: list[dict[str, Any]],
    chain_id: str,
    checkpoint_index: int,
    chain_round: int,
    previous_response_id: str | None,
    max_output_tokens: int | None,
    server_compaction_threshold: int | None,
) -> RuntimeResponseTrace:
    response_trace = _runtime_response_trace_payload(response, requested_model=requested_model)
    usage = response_trace.usage
    dump_v2_debug(
        run_id=str(generation_run_id),
        phase=phase,
        agent_name=agent_name,
        dump_type=f"round-{round_idx + 1:02d}-response",
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
            hosted_followup=None,
            max_output_tokens=max_output_tokens,
            server_compaction_threshold=server_compaction_threshold,
        ),
    )
    _set_response_run_outputs(
        run_tree,
        response=response,
        file_search_calls=file_search_calls,
        web_search_calls=web_search_calls,
        trace_payload=response_trace,
    )
    await _save_response_create_trace(
        db,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
        prompt=prompt,
        prompt_hash=prompt_hash,
        round_number=round_idx + 1,
        tools=tools,
        policy=policy,
        chain_id=chain_id,
        checkpoint_index=checkpoint_index,
        chain_round=chain_round,
        previous_response_id=previous_response_id,
        requested_model=requested_model,
        response=response,
        file_search_calls=file_search_calls,
        web_search_calls=web_search_calls,
        trace_payload=response_trace,
        latency_ms=latency_ms,
        max_output_tokens=max_output_tokens,
        server_compaction_threshold=server_compaction_threshold,
    )
    return response_trace


__all__ = ["record_round_response_trace"]
