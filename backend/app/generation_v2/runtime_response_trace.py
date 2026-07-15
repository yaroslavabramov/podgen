from __future__ import annotations

from app.generation_v2.contracts import V2ToolName
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.runtime_debug import tool_policy_payload as _tool_policy_payload, tool_trace_name as _tool_trace_name
from app.generation_v2.runtime_response import compact_hosted_calls as _compact_hosted_calls
from app.generation_v2.runtime_response_status import (
    response_error,
    response_incomplete_reason,
    response_output_statuses,
)
from app.observability import safe_set_run
from app.services.llm import (
    _openai_response_model,
    _openai_token_cost_payload,
    _openai_trace_usage_payload,
    _openai_usage,
)


@dataclass(frozen=True)
class RuntimeResponseTrace:
    usage: dict[str, Any]
    response_model: str | None
    billing_model: str
    token_cost_payload: dict[str, Any]
    estimated_token_cost_usd: float | None
    trace_usage_payload: dict[str, Any]


def runtime_response_trace_payload(response: Any, *, requested_model: str) -> RuntimeResponseTrace:
    usage = _openai_usage(response)
    response_model = _openai_response_model(response)
    billing_model = response_model or requested_model
    token_cost_payload = _openai_token_cost_payload(billing_model, usage)
    return RuntimeResponseTrace(
        usage=usage,
        response_model=response_model,
        billing_model=billing_model,
        token_cost_payload=token_cost_payload,
        estimated_token_cost_usd=token_cost_payload["estimated_token_cost_usd"],
        trace_usage_payload=_openai_trace_usage_payload(
            requested_model=requested_model,
            response_model=response_model,
            billing_model=billing_model,
            usage=usage,
            token_cost_payload=token_cost_payload,
        ),
    )


def set_response_run_outputs(
    run_tree: Any,
    *,
    response: Any,
    file_search_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    trace_payload: RuntimeResponseTrace,
) -> None:
    safe_set_run(
        run_tree,
        outputs={
            "response_id": response.id,
            "status": getattr(response, "status", None),
            "incomplete_reason": response_incomplete_reason(response),
            "provider_error": response_error(response),
            "output_item_statuses": response_output_statuses(response),
            "output_text_chars": len(response.output_text or ""),
            "file_search_call_count": len(file_search_calls),
            "web_search_call_count": len(web_search_calls),
            **trace_payload.trace_usage_payload,
            **trace_payload.usage,
        },
    )


async def save_response_create_trace(
    db: AsyncSession,
    *,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    prompt: str,
    prompt_hash: str,
    round_number: int,
    tools: list[dict[str, Any]],
    policy: Any,
    chain_id: str,
    checkpoint_index: int,
    chain_round: int,
    previous_response_id: str | None,
    requested_model: str,
    response: Any,
    file_search_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    trace_payload: RuntimeResponseTrace,
    latency_ms: float,
    max_output_tokens: int | None,
) -> None:
    await save_tool_trace(
        db,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
        tool_name=V2ToolName.OPENAI_RESPONSES_CREATE,
        input_json={
            "prompt_chars": len(prompt),
            "prompt_hash": prompt_hash,
            "round": round_number,
            "tools": [_tool_trace_name(tool) for tool in tools],
            "allowed_tool_names": [_tool_trace_name(tool) for tool in tools],
            "tool_policy": _tool_policy_payload(policy),
            "chain_id": chain_id,
            "checkpoint_index": checkpoint_index,
            "chain_round": chain_round,
            "has_previous_response_id": bool(previous_response_id),
            "requested_model": requested_model,
            "max_output_tokens": max_output_tokens,
        },
        output_json={
            "response_id": response.id,
            "requested_model": requested_model,
            "response_model": trace_payload.response_model,
            "billing_model": trace_payload.billing_model,
            "status": getattr(response, "status", None),
            "incomplete_reason": response_incomplete_reason(response),
            "provider_error": response_error(response),
            "output_item_statuses": response_output_statuses(response),
            "output_text_chars": len(response.output_text or ""),
            "file_search_call_count": len(file_search_calls),
            "web_search_call_count": len(web_search_calls),
            "file_search_calls": _compact_hosted_calls(file_search_calls),
            "web_search_calls": _compact_hosted_calls(web_search_calls),
            "chain_id": chain_id,
            "checkpoint_index": checkpoint_index,
            "chain_round": chain_round,
            "max_output_tokens": max_output_tokens,
            "usage": trace_payload.trace_usage_payload,
            **trace_payload.token_cost_payload,
            **trace_payload.trace_usage_payload,
            **trace_payload.usage,
        },
        provider_call_id=response.id,
        latency_ms=latency_ms,
        input_tokens=trace_payload.usage.get("input_tokens") or trace_payload.usage.get("prompt_tokens"),
        output_tokens=trace_payload.usage.get("output_tokens") or trace_payload.usage.get("completion_tokens"),
        cost_usd=trace_payload.estimated_token_cost_usd,
    )
