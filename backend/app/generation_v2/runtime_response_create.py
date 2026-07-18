from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import openai

from app.generation_v2.debug_dump import dump_v2_debug
from app.generation_v2.runtime_debug import (
    redact_runtime_file_ids as _redact_runtime_file_ids,
    tool_policy_payload as _tool_policy_payload,
    tool_trace_name as _tool_trace_name,
)
from app.observability import safe_set_run, trace_span


@dataclass(frozen=True)
class RuntimeResponseCreateResult:
    response: Any
    run_tree: Any
    latency_ms: float


async def create_runtime_response(
    *,
    client: Any,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    round_number: int,
    prompt_hash: str,
    system: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    tools: list[dict[str, Any]],
    policy: Any,
    input_payload: Any,
    previous_response_id: str | None,
    requested_model: str,
    reasoning_effort: str | None,
    vector_store_id: str | None,
    local_tools: list[Any] | None,
    max_output_tokens: int | None,
    server_compaction_threshold: int | None,
    temperature: float | None,
    chain_id: str,
    checkpoint_index: int,
    chain_round: int,
) -> RuntimeResponseCreateResult:
    started = time.perf_counter()
    with trace_span(
        f"generation_v2.{phase}.{agent_name}.responses_create",
        run_type="llm",
        inputs={
            "prompt_chars": len(prompt),
            "prompt_hash": prompt_hash,
            "round": round_number,
            "schema": schema_name,
        },
        metadata={
            "provider": "openai",
            "model": requested_model,
            "phase": phase,
            "agent_name": agent_name,
            "reasoning_effort": reasoning_effort,
            "vector_store_id": vector_store_id or "",
            "server_compaction_threshold": server_compaction_threshold,
        },
        tags=["generation-v2", "openai", "responses", f"phase:{phase}", f"agent:{agent_name}"],
    ) as run_tree:
        try:
            response = await client.responses.create(
                model=requested_model,
                instructions=system,
                input=input_payload,
                previous_response_id=previous_response_id or openai.NOT_GIVEN,
                max_output_tokens=(
                    max_output_tokens if max_output_tokens is not None else openai.NOT_GIVEN
                ),
                context_management=(
                    [
                        {
                            "type": "compaction",
                            "compact_threshold": server_compaction_threshold,
                        }
                    ]
                    if server_compaction_threshold is not None
                    else openai.NOT_GIVEN
                ),
                temperature=temperature if temperature is not None else openai.NOT_GIVEN,
                reasoning={"effort": reasoning_effort} if reasoning_effort else openai.NOT_GIVEN,
                tools=tools or openai.NOT_GIVEN,
                parallel_tool_calls=(
                    policy.parallel_local_tool_calls if local_tools else openai.NOT_GIVEN
                ),
                include=["file_search_call.results"] if vector_store_id else openai.NOT_GIVEN,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
            )
        except Exception as exc:
            exception_payload = _redact_runtime_file_ids(
                {
                    "run_id": str(generation_run_id),
                    "phase": phase,
                    "agent_name": agent_name,
                    "round": round_number,
                    "prompt_hash": prompt_hash,
                    "system": system,
                    "prompt": prompt,
                    "schema_name": schema_name,
                    "schema": schema,
                    "tools": [_tool_trace_name(tool) for tool in tools],
                    "tool_policy": _tool_policy_payload(policy),
                    "input_payload": input_payload,
                    "chain_id": chain_id,
                    "checkpoint_index": checkpoint_index,
                    "chain_round": chain_round,
                    "requested_model": requested_model,
                    "server_compaction_threshold": server_compaction_threshold,
                    "error": str(exc),
                }
            )
            dump_v2_debug(
                run_id=str(generation_run_id),
                phase=phase,
                agent_name=agent_name,
                dump_type=f"round-{round_number:02d}-exception",
                payload=exception_payload,
            )
            safe_set_run(
                run_tree,
                outputs={"status": "exception", "error": exception_payload["error"]},
            )
            raise
    return RuntimeResponseCreateResult(
        response=response,
        run_tree=run_tree,
        latency_ms=(time.perf_counter() - started) * 1000,
    )
