"""run-cost-budget/1 on a real journal: receipt charges, an unsent stage refusal and an explicit grant."""

import json
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation import cost_budget
from podgen.runtime.evaluation.contracts import RunFailure, WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.evaluation.total_input_recovery import effective_cost_limit
from tests.research_material_provider import ResearchProvider
from tests.test_research_material_resolver import fixture


def start(repo, scope, producer, monkeypatch):
    materials, _, acquire, _ = fixture(repo, scope, producer, monkeypatch)
    profiles = {}
    for role, model, provider, roles in (
        ("writer", "claude-opus-5-5", "anthropic", ["writer"]),
        ("reviewer", "gpt-6.1-terra", "openai", ["editorial_reviewer", "plan_reviewer"]),
        ("planner", "gpt-6.1-sol", "openai", ["planner"]),
    ):
        profiles[role] = create_profile(
            repo,
            scope[0],
            model=model,
            provider=provider,
            roles=roles,
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
        ).ref
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        case_id=materials.packet.assignment.key,
        input_contract="editorial-role-packets/1",
        author_ref=materials.shared_ref,
        writer_profile_ref=profiles["writer"],
        reviewer_profile_ref=profiles["reviewer"],
        planner_profile_ref=profiles["planner"],
        plan_reviewer_profile_ref=profiles["reviewer"],
        behavior_profile="production/1",
        plan_mode="own-plan",
        review_mode="independent-readings/1",
        max_plan_revisions=1,
        max_script_revisions=2,
    )
    provider = ResearchProvider()
    return command, provider, WritingEvaluation(repo, lambda profile: provider, material_acquirer=acquire)


def expected_cost(provider):
    # The local double reports 100 uncached input and 100 output tokens per response.
    rates = cost_budget.RATES
    return sum(
        int(100 * float(rates[body["model"]]["input"]) + 100 * float(rates[body["model"]]["output"]))
        for body in provider.requests
    )


def spent(journal, run_id):
    journal.execution.invalidate()
    return journal.execution.current_head(run_id)["accounting"].get(cost_budget.FIELD, 0)


def test_receipts_charge_the_pinned_usd_budget_instead_of_tokens(repo, scope, producer, monkeypatch):
    command, provider, evaluation = start(repo, scope, producer, monkeypatch)
    result = evaluation.run(command)
    journal = EvaluationJournal(repo, scope[0])
    budget = cost_budget.active(journal.manifest(command.run_id))
    assert budget["limit_microusd"] == 5_000_000
    assert set(budget["rates"]) == {"claude-opus-5-5", "gpt-6.1-sol", "gpt-6.1-terra"}
    assert result["status"] != "needs_attention"
    assert spent(journal, command.run_id) == expected_cost(provider)
    count = len(provider.requests)
    evaluation.run(command)
    assert len(provider.requests) == count


def test_stage_ceiling_refuses_before_dispatch_and_an_explicit_grant_resumes(
    repo, scope, producer, monkeypatch
):
    monkeypatch.setattr(cost_budget, "LIMIT_USD", "0.40")
    command, provider, evaluation = start(repo, scope, producer, monkeypatch)
    journal = EvaluationJournal(repo, scope[0])
    try:
        evaluation.run(command)
    except RunFailure as error:
        assert error.code == "run_cost_budget_exceeded"
    else:
        raise AssertionError("the planning ceiling must refuse the first maximum reservation")
    assert provider.requests == []
    refused = journal.invocation(command.run_id, "plan")
    assert refused["status"] == "validation_failed"
    assert spent(journal, command.run_id) == 0
    evaluation.run(command, retry_total_input_budget=refused["invocation_id"], max_total_usd="20")
    grant = journal.invocation(command.run_id, "plan")
    authorization = journal.read(
        RuntimeRef(record_kind="tool_execution", record_id=grant["request_record_id"])
    )["authorization"]
    assert authorization["kind"] == "run-cost-budget-retry/1"
    assert authorization["max_limit_microusd"] == 20_000_000
    assert grant["attempt_kind"] == "total_input_retry"
    assert effective_cost_limit(journal, command.run_id) == 20_000_000
    assert journal.run(command.run_id)["status"] != "needs_attention"
    assert spent(journal, command.run_id) == expected_cost(provider)
    count = len(provider.requests)
    evaluation.run(command)
    assert len(provider.requests) == count
    assert json.dumps(journal.manifest(command.run_id)[cost_budget.KEY]["limit_microusd"]) == "400000"
