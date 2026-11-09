"""Production rejects a model registered at the wrong provider; archived replay remains unchanged."""

from copy import copy

import pytest
from podgen.domain.errors import ContractError
from podgen.writing.behavior import resolve_behavior

from tests.test_v5_role_wire import fixture


def test_production_model_registration_requires_its_provider_and_the_canonical_endpoint_adapter():
    _, _, _, template, *_ = fixture()
    production = resolve_behavior("production/1")
    historical = resolve_behavior("narrative-rebuild/1")
    for stage, model, provider in (
        ("draft", "claude-opus-5-5", "anthropic"),
        ("revise-1", "claude-opus-5-5", "anthropic"),
        ("plan", "gpt-6.1-sol", "openai"),
        ("plan-revise", "gpt-6.1-sol", "openai"),
        ("plan-review", "gpt-6.1-terra", "openai"),
        ("plan-review-revised", "gpt-6.1-terra", "openai"),
        ("reader-review", "gpt-6.1-terra", "openai"),
        ("fact-review", "gpt-6.1-terra", "openai"),
        ("anchor-repair", "gpt-6.1-terra", "openai"),
        ("plan-target-repair", "gpt-6.1-terra", "openai"),
    ):
        profile = copy(template)
        profile.model_revision_or_alias = model
        profile.context_limit, profile.output_limit = 200000, 24576
        profile.generation_parameters = dict(
            reasoning=dict(effort="medium"), text=dict(verbosity="high"), store=False, background=False
        )
        profile.provider = provider
        profile.api_family = "messages" if provider == "anthropic" else "responses"
        profile.adapter_version = (
            "anthropic-messages-v1" if provider == "anthropic" else "openai-responses-v1"
        )
        production.validate_model(stage, profile)
        wrong = copy(profile)
        wrong.provider = "openai" if provider == "anthropic" else "anthropic"
        wrong.api_family = "responses" if wrong.provider == "openai" else "messages"
        wrong.adapter_version = (
            "openai-responses-v1" if wrong.provider == "openai" else "anthropic-messages-v1"
        )
        with pytest.raises(ContractError, match="behavior_model_profile_mismatch"):
            production.validate_model(stage, wrong)
        # In particular, preserve the archived OpenAI registration of the Claude model.
        historical.validate_model(stage, wrong)
        wrong_endpoint = copy(profile)
        wrong_endpoint.api_family = "responses" if provider == "anthropic" else "messages"
        with pytest.raises(ContractError, match="unsupported_evaluation_profile"):
            production.validate_model(stage, wrong_endpoint)
