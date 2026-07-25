from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_v2 import V2ToolTrace


async def summarize_tool_trace_costs(db: AsyncSession, generation_run_id) -> dict[str, Any]:
    result = await db.execute(
        select(V2ToolTrace)
        .where(V2ToolTrace.generation_run_id == generation_run_id)
        .order_by(V2ToolTrace.created_at.asc())
    )
    groups: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    warning: str | None = None
    max_call_cost_usd = 0.0
    for trace in result.scalars():
        output = trace.output_json or {}
        usage = output.get("usage") if isinstance(output.get("usage"), dict) else output
        requested_model = str((trace.input_json or {}).get("requested_model") or output.get("requested_model") or "")
        response_model = str(output.get("response_model") or "")
        billing_model = str(output.get("billing_model") or response_model or requested_model or "")
        has_billable_usage = bool(
            trace.input_tokens
            or trace.output_tokens
            or trace.cost_usd
            or (usage or {}).get("input_tokens")
            or (usage or {}).get("prompt_tokens")
            or (usage or {}).get("output_tokens")
            or (usage or {}).get("completion_tokens")
            or output.get("estimated_token_cost_usd")
        )
        if not has_billable_usage:
            continue
        key = (trace.phase, trace.agent_name, trace.tool_name, requested_model, response_model, billing_model)
        row = groups.setdefault(
            key,
            {
                "phase": trace.phase,
                "agent_name": trace.agent_name,
                "tool_name": trace.tool_name,
                "requested_model": requested_model,
                "response_model": response_model,
                "billing_model": billing_model,
                "call_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "uncached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "estimated_token_cost_usd": 0.0,
            },
        )
        row["call_count"] += 1
        input_tokens = int((usage or {}).get("input_tokens") or (usage or {}).get("prompt_tokens") or trace.input_tokens or 0)
        cached_tokens = int((usage or {}).get("cached_input_tokens") or 0)
        output_tokens = int((usage or {}).get("output_tokens") or (usage or {}).get("completion_tokens") or trace.output_tokens or 0)
        uncached_tokens = int((usage or {}).get("uncached_input_tokens") or max(0, input_tokens - cached_tokens))
        row["input_tokens"] += input_tokens
        row["cached_input_tokens"] += cached_tokens
        row["uncached_input_tokens"] += uncached_tokens
        row["output_tokens"] += output_tokens
        row["reasoning_output_tokens"] += int((usage or {}).get("reasoning_output_tokens") or 0)
        call_cost_usd = float(output.get("estimated_token_cost_usd") or trace.cost_usd or 0.0)
        row["estimated_token_cost_usd"] += call_cost_usd
        max_call_cost_usd = max(max_call_cost_usd, call_cost_usd)
        warning = warning or output.get("cost_warning")
    rows = list(groups.values())
    rows.sort(key=lambda item: (item["phase"], item["agent_name"], item["tool_name"], item["billing_model"]))
    totals = {
        "call_count": sum(row["call_count"] for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows),
        "uncached_input_tokens": sum(row["uncached_input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "reasoning_output_tokens": sum(row["reasoning_output_tokens"] for row in rows),
        "estimated_token_cost_usd": round(sum(float(row["estimated_token_cost_usd"] or 0.0) for row in rows), 8),
        "max_call_cost_usd": round(max_call_cost_usd, 8),
    }
    for row in rows:
        row["estimated_token_cost_usd"] = round(float(row["estimated_token_cost_usd"] or 0.0), 8)
    return {
        "cost_basis": "openai_token_usage_only",
        "cost_warning": warning
        or "Token-cost estimate only; this is not an OpenAI invoice total and can exclude hosted-tool, file/vector storage or search, retry, tax, credit, and adjustment charges.",
        "totals": totals,
        "groups": rows,
    }


__all__ = ["summarize_tool_trace_costs"]
