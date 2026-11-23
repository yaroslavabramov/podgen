"""Run cost budget in USD: pinned public rates, receipt charges and pre-submit reservations.

run-cost-budget/1 replaces the run-wide token ceilings of a new production run. Each physical
generation reserves its maximum possible charge before dispatch: the counted input at the
uncached cache-write rate plus the profile output limit. The completed receipt replaces that
reservation with its actual charge. Stage groups have cumulative ceilings, so planning cannot
consume what the first factual reading needs; an earlier group's unused share carries forward.
"""

from decimal import ROUND_CEILING, Decimal

from podgen.domain.errors import ContractError

KEY = "run_cost_budget"
VERSION = "run-cost-budget/1"
FIELD = "cost_microusd"
LIMIT_USD = "5.00"
# Cumulative share of the limit available once a stage of the group starts.
STAGE_GROUPS = (
    ("planning", "0.30"),
    ("draft", "0.55"),
    ("first_reading", "0.80"),
    ("revision", "1.00"),
)
CHECKED_AT = "2026-11-23"
WEB_SEARCH_RESERVED_CALLS = 5
# USD per million tokens, Standard processing; web search is USD per completed call.
RATES = {
    "gpt-6.1-terra": dict(
        provider="openai",
        input="1.00",
        cached_input="0.05",
        cache_write=dict(ttl_30m="1.25", ttl_12h="2.50"),
        output="5.00",
        web_search_call="0.01",
        long_context=dict(above_input_tokens=272000, input_multiplier="2", output_multiplier="1.5"),
        source="https://developers.openai.com/api/docs/models/gpt-6.1-terra",
    ),
    "gpt-6.1-sol": dict(
        provider="openai",
        input="2.00",
        cached_input="0.10",
        # Only the 30-minute write price is published; 12 hours uses Terra's 2x ratio.
        cache_write=dict(ttl_30m="2.50", ttl_12h="5.00"),
        output="10.00",
        web_search_call="0.01",
        long_context=dict(above_input_tokens=272000, input_multiplier="2", output_multiplier="1.5"),
        source="https://developers.openai.com/api/docs/models/gpt-6.1-sol",
    ),
    "claude-opus-5-5": dict(
        provider="anthropic",
        input="4.00",
        cached_input="0.20",
        # The Messages adapter writes every cache breakpoint with ttl=5m.
        cache_write=dict(ttl_5m="5.00", ttl_1h="8.00"),
        adapter_cache_ttl="ttl_5m",
        output="20.00",
        source="https://platform.claude.com/docs/en/about-claude/pricing",
    ),
}
MICRO = Decimal(1_000_000)


def policy(models, *, limit_usd=None, spent_before_microusd=0):
    """Pin rates for every model of the run, or record explicitly that it cannot be priced."""
    limit_usd = LIMIT_USD if limit_usd is None else limit_usd
    models = sorted(set(models))
    unpriced = [model for model in models if model not in RATES]
    if unpriced:
        return dict(version=VERSION, status="unpriced", unpriced_models=unpriced)
    limit = microusd(Decimal(limit_usd)) - spent_before_microusd
    if limit <= 0:
        raise ContractError("run_cost_budget_exhausted_by_parent")
    return dict(
        version=VERSION,
        status="active",
        limit_microusd=limit,
        spent_before_microusd=spent_before_microusd,
        stage_groups=[dict(group=group, cumulative_share=share) for group, share in STAGE_GROUPS],
        web_search_reserved_calls=WEB_SEARCH_RESERVED_CALLS,
        rates={model: RATES[model] for model in models},
        checked_at=CHECKED_AT,
    )


def active(manifest):
    value = manifest.get(KEY)
    if value is None or value.get("status") == "unpriced":
        return None
    if value.get("version") != VERSION or value.get("status") != "active":
        raise ContractError("unknown_run_cost_budget")
    return value


def microusd(value):
    return int((Decimal(value) * MICRO).to_integral_value(rounding=ROUND_CEILING))


def _tokens(count, rate, multiplier="1"):
    # A rate in USD per million tokens equals micro-USD per token.
    return Decimal(count) * Decimal(rate) * Decimal(multiplier)


def _rates(value, model):
    rates = value["rates"].get(model)
    if rates is None:
        raise ContractError("run_cost_model_unpriced")
    return rates


def _long(rates, input_tokens):
    rule = rates.get("long_context")
    if rule and input_tokens > rule["above_input_tokens"]:
        return rule["input_multiplier"], rule["output_multiplier"]
    return "1", "1"


def charge(value, model, response):
    """Actual charge of one completed response from its normalized usage receipt."""
    rates = _rates(value, model)
    usage = response.get("usage") or {}
    total, output = usage.get("input_tokens"), usage.get("output_tokens")
    if type(total) is not int or type(output) is not int:
        raise ContractError("run_cost_usage_missing")
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens") or 0
    written = details.get("cache_write_tokens") or 0
    if rates["provider"] == "anthropic":
        writes = {rates["adapter_cache_ttl"]: written}
    else:
        ttl = details.get("cache_write_tokens_details") or {}
        writes = {key: ttl.get(key + "_tokens", 0) for key in rates["cache_write"]}
        if sum(writes.values()) != written:
            # An unattributed write is priced at the most expensive retention.
            writes = {max(rates["cache_write"], key=lambda key: Decimal(rates["cache_write"][key])): written}
    uncached = total - cached - written
    if min(uncached, cached, written, output) < 0:
        raise ContractError("run_cost_usage_inconsistent")
    input_multiplier, output_multiplier = _long(rates, total)
    amount = (
        _tokens(uncached, rates["input"], input_multiplier)
        + _tokens(cached, rates["cached_input"], input_multiplier)
        + sum(_tokens(count, rates["cache_write"][key], input_multiplier) for key, count in writes.items())
        + _tokens(output, rates["output"], output_multiplier)
    )
    searches = sum(
        1
        for item in response.get("output") or ()
        if isinstance(item, dict)
        and item.get("type") == "web_search_call"
        and item.get("status") == "completed"
    )
    if searches:
        amount += Decimal(searches) * Decimal(rates["web_search_call"]) * MICRO
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def reservation(value, model, input_tokens, output_limit, *, web_search=False):
    """Largest charge this request can produce: no cache hit, full output, bounded web search."""
    rates = _rates(value, model)
    input_multiplier, output_multiplier = _long(rates, input_tokens)
    write = max(Decimal(rate) for rate in rates["cache_write"].values())
    amount = _tokens(input_tokens, max(write, Decimal(rates["input"])), input_multiplier) + _tokens(
        output_limit, rates["output"], output_multiplier
    )
    if web_search:
        # Native search has no provider-side call limit; reserve a pinned allowance per response.
        amount += Decimal(value["web_search_reserved_calls"]) * Decimal(rates["web_search_call"]) * MICRO
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def group(stage):
    if stage.startswith("plan"):
        return "planning"
    if stage == "draft":
        return "draft"
    if stage in ("reader-review", "fact-review"):
        return "first_reading"
    return "revision"


def stage_ceiling(value, stage, limit_microusd=None):
    limit = value["limit_microusd"] if limit_microusd is None else limit_microusd
    share = next(row["cumulative_share"] for row in value["stage_groups"] if row["group"] == group(stage))
    return int((Decimal(limit) * Decimal(share)).to_integral_value(rounding=ROUND_CEILING))
