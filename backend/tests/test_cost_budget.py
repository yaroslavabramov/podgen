"""USD pricing of receipts, maximum reservations and cumulative stage ceilings; no DB."""

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import cost_budget

MODELS = ("claude-opus-5-5", "gpt-6.1-sol", "gpt-6.1-terra")


def budget(**options):
    return cost_budget.policy(MODELS, **options)


def openai(input_tokens, cached, written, output, *, ttl=None, searches=0):
    details = dict(cached_tokens=cached, cache_write_tokens=written)
    if ttl is not None:
        details["cache_write_tokens_details"] = ttl
    return dict(
        usage=dict(input_tokens=input_tokens, input_tokens_details=details, output_tokens=output),
        output=[dict(type="web_search_call", status="completed")] * searches,
    )


def test_policy_pins_rates_and_stage_shares_for_the_configured_models():
    value = budget()
    assert value["status"] == "active" and value["limit_microusd"] == 5_000_000
    assert set(value["rates"]) == set(MODELS)
    assert [row["group"] for row in value["stage_groups"]] == [
        "planning",
        "draft",
        "first_reading",
        "revision",
    ]
    assert cost_budget.policy(["gpt-6.1-terra", "local-double"]) == dict(
        version=cost_budget.VERSION, status="unpriced", unpriced_models=["local-double"]
    )
    assert cost_budget.active(dict(run_cost_budget=cost_budget.policy(["local-double"]))) is None


def test_terra_receipt_prices_cached_written_uncached_output_and_web_search():
    # Atlantic fact-review turn shape: almost all input is a cache read.
    response = openai(54_589, 52_000, 2_500, 120, ttl=dict(ttl_30m_tokens=2_500), searches=2)
    expected = 89 * 1.00 + 52_000 * 0.05 + 2_500 * 1.25 + 120 * 5.00 + 2 * 10_000
    assert cost_budget.charge(budget(), "gpt-6.1-terra", response) == pytest.approx(expected, abs=1)


def test_unattributed_writes_use_the_most_expensive_retention():
    response = openai(10_000, 0, 10_000, 0)
    assert cost_budget.charge(budget(), "gpt-6.1-sol", response) == 10_000 * 5


def test_anthropic_writes_use_the_adapter_retention():
    response = dict(
        usage=dict(
            input_tokens=50_421,
            input_tokens_details=dict(cached_tokens=40_000, cache_write_tokens=10_421),
            output_tokens=2_000,
        )
    )
    assert cost_budget.charge(budget(), "claude-opus-5-5", response) == 40_000 * 0.2 + 10_421 * 5 + 2_000 * 20


def test_long_context_requests_pay_the_published_multipliers():
    response = openai(300_000, 0, 0, 1_000)
    assert cost_budget.charge(budget(), "gpt-6.1-terra", response) == 300_000 * 2 + 1_000 * 7.5


def test_inconsistent_or_missing_usage_is_explicit():
    with pytest.raises(ContractError, match="run_cost_usage_inconsistent"):
        cost_budget.charge(budget(), "gpt-6.1-terra", openai(10, 20, 0, 0))
    with pytest.raises(ContractError, match="run_cost_usage_missing"):
        cost_budget.charge(budget(), "gpt-6.1-terra", dict(usage={}))
    with pytest.raises(ContractError, match="run_cost_model_unpriced"):
        cost_budget.charge(budget(), "other", openai(1, 0, 0, 1))


def test_reservation_is_the_largest_possible_charge():
    value = budget()
    reserved = cost_budget.reservation(value, "gpt-6.1-terra", 55_000, 24_576, web_search=True)
    assert reserved == 55_000 * 2.50 + 24_576 * 5 + 5 * 10_000
    worst = openai(55_000, 0, 55_000, 24_576, ttl=dict(ttl_12h_tokens=55_000), searches=5)
    assert cost_budget.charge(value, "gpt-6.1-terra", worst) <= reserved


def test_cumulative_ceilings_keep_the_first_reading_share():
    value = budget()
    assert cost_budget.stage_ceiling(value, "plan-review-revised") == 1_500_000
    assert cost_budget.stage_ceiling(value, "draft") == 2_750_000
    assert cost_budget.stage_ceiling(value, "fact-review") == 4_000_000
    assert cost_budget.stage_ceiling(value, "fact-review-2") == 5_000_000
    assert cost_budget.stage_ceiling(value, "fact-review", 10_000_000) == 8_000_000


def test_material_continuation_receives_only_the_unspent_limit():
    assert budget(spent_before_microusd=1_250_000)["limit_microusd"] == 3_750_000
    with pytest.raises(ContractError, match="run_cost_budget_exhausted_by_parent"):
        budget(spent_before_microusd=5_000_000)
