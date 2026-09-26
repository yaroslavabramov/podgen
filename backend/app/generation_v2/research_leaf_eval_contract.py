from __future__ import annotations

from typing import Any

from app.generation_v2.artifact_costs import summarize_tool_trace_costs
from app.generation_v2.contracts import V2AgentPrefix
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.triage_eval_support import (
    EvalBudgetExceeded,
    TRIAGE_EVAL_ALLOWED_MODELS,
    TRIAGE_EVAL_ALLOWED_MODELS_LABEL,
    TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS,
)


RESEARCH_LEAF_EVAL_MAX_COST_USD = 1.70
RESEARCH_LEAF_EVAL_DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
RESEARCH_LEAF_EVAL_DEFAULT_REASONING_EFFORT = "xhigh"
RESEARCH_LEAF_EVAL_ALLOWED_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}


class ResearchLeafEvalRuntime(V2Runtime):
    """Eval-only runtime override for one isolated research job."""

    def __init__(
        self,
        *,
        model_override: str,
        reasoning_effort: str,
        before_provider_call,
    ) -> None:
        super().__init__(before_provider_call=before_provider_call)
        self.eval_model_override = model_override
        self.reasoning_effort = reasoning_effort

    def model_for(self, phase: str, agent_name: str) -> str:
        if str(agent_name).startswith(V2AgentPrefix.RESEARCH_JOB.value):
            return self.eval_model_override
        return super().model_for(phase, agent_name)


class ResearchLeafEvalCostGuard:
    """Budget guard for one paid leaf run; existing triage cost is baseline, not spendable budget."""

    def __init__(self, *, max_delta_cost_usd: float, baseline_cost_usd: float) -> None:
        self.max_delta_cost_usd = max_delta_cost_usd
        self.baseline_cost_usd = baseline_cost_usd

    async def __call__(
        self,
        *,
        db: Any,
        generation_run_id: Any,
        requested_model: str,
        max_output_tokens: int | None,
        server_compaction_threshold: int | None,
        **_kwargs: Any,
    ) -> None:
        normalized_model = str(requested_model or "").casefold()
        if normalized_model not in TRIAGE_EVAL_ALLOWED_MODELS:
            raise EvalBudgetExceeded(
                f"Research leaf eval budget contract supports only {TRIAGE_EVAL_ALLOWED_MODELS_LABEL}; "
                f"received {requested_model or '<missing>'}"
            )
        effective_output_limit = (
            int(max_output_tokens)
            if max_output_tokens is not None
            else TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS
        )
        if effective_output_limit > TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS:
            raise EvalBudgetExceeded("Research leaf eval output bound exceeds the audited budget contract")
        # Research job agents currently may not set server-side compaction. Unlike the
        # triage eval, that must not make the guard reject before the first model call.
        _ = server_compaction_threshold
        report = await summarize_tool_trace_costs(db, generation_run_id)
        observed_total = float(report["totals"].get("estimated_token_cost_usd") or 0.0)
        observed_delta = max(0.0, observed_total - self.baseline_cost_usd)
        reserve = float(report["totals"].get("max_call_cost_usd") or 0.0)
        if observed_delta + reserve > self.max_delta_cost_usd:
            raise EvalBudgetExceeded(
                f"Research leaf eval stopped before the next provider call: ${observed_delta:.8f} leaf spend, "
                f"${reserve:.8f} observed-call reserve, ${self.max_delta_cost_usd:.2f} leaf ceiling"
            )


def validate_eval_model_and_effort(model: str, reasoning_effort: str) -> tuple[str, str]:
    normalized_model = str(model or "").strip()
    if normalized_model.casefold() not in TRIAGE_EVAL_ALLOWED_MODELS:
        raise ValueError(
            f"Research leaf eval supports only {TRIAGE_EVAL_ALLOWED_MODELS_LABEL}; "
            f"received {normalized_model or '<missing>'}"
        )
    normalized_effort = str(reasoning_effort or "").strip().lower()
    if normalized_effort not in RESEARCH_LEAF_EVAL_ALLOWED_REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be one of "
            + ", ".join(sorted(RESEARCH_LEAF_EVAL_ALLOWED_REASONING_EFFORTS))
        )
    return normalized_model, normalized_effort


__all__ = [
    "RESEARCH_LEAF_EVAL_DEFAULT_MODEL",
    "RESEARCH_LEAF_EVAL_DEFAULT_REASONING_EFFORT",
    "RESEARCH_LEAF_EVAL_MAX_COST_USD",
    "ResearchLeafEvalCostGuard",
    "ResearchLeafEvalRuntime",
    "validate_eval_model_and_effort",
]
