"""Explicit targeted recovery keeps paid stages intact and authorizes exactly one physical attempt."""

import pytest

from podgen.runtime.evaluation.contracts import RunFailure, RunLimits
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation
from tests.credit_provider import CreditProvider
from tests.test_writing_evaluation import FakeProvider
from tests.test_writing_evaluation import setup_writer as setup_writer


def app(repo, loader, provider):
    return WritingEvaluation(repo, lambda profile: provider, materials_loader=loader)


@pytest.mark.parametrize("stage", ["draft", "review", "revise"])
def test_explicit_stage_retry_preserves_input_and_is_idempotent(repo, setup_writer, stage, tmp_path):
    command, loader, _ = setup_writer
    provider = CreditProvider(FakeProvider(), stage=stage)
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure, match="provider_http_429"):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    original = journal.invocation(command.run_id, stage)
    saved = journal.request(original)
    events = journal.events(original)
    with pytest.raises(RunFailure):
        runner.run(command)
    calls = len(provider.requests)
    result = runner.run(command, retry_credit_refusal=original["invocation_id"])
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 4
    assert result["token_usage_or_reservation"] == dict(input=400, output=369)
    assert len(provider.requests) > calls
    attempts = journal.attempts(command.run_id, stage)
    assert [row["attempt_kind"] for row in attempts] == ["role", "credit_retry"]
    assert [row["status"] for row in attempts] == ["failed", "completed"]
    retried = journal.request(attempts[1])
    assert {k: v for k, v in retried.items() if k != "metadata"} == {
        k: v for k, v in saved.items() if k != "metadata"
    }
    assert retried["metadata"]["podgen_invocation_id"] != saved["metadata"]["podgen_invocation_id"]
    assert journal.request(original) == saved and journal.events(original) == events
    assert runner.run(command, retry_credit_refusal=original["invocation_id"]) == result
    assert runner.run(command) == result
    assert len(provider.requests) == 4
    exported = export_run(repo, command.project_id, command.run_id, tmp_path / "export")
    assert exported["input_tokens"] == 400 and exported["output_tokens_known"] == 369
    assert not exported["unknown_usage_invocations"]
    assert len(exported["invocations"][stage]["attempts"]) == 2
    assert (tmp_path / f"export/archive/{stage}/attempt-1/request.json").is_file()
    with pytest.raises(RunFailure, match="credit_refusal_retry_not_eligible"):
        runner.run(command, retry_credit_refusal=attempts[1]["invocation_id"])
    assert len(provider.requests) == 4


def test_a_second_credit_refusal_needs_a_new_targeted_action(repo, setup_writer):
    command, loader, _ = setup_writer
    provider = CreditProvider(FakeProvider(), remaining=2)
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    first = journal.invocation(command.run_id, "review")
    with pytest.raises(RunFailure, match="provider_http_429"):
        runner.run(command, retry_credit_refusal=first["invocation_id"])
    second = journal.invocation(command.run_id, "review")
    assert second["attempt_index"] == 1
    calls = len(provider.requests)
    for target in (None, first["invocation_id"]):
        with pytest.raises(RunFailure):
            runner.run(command, retry_credit_refusal=target)
        assert len(provider.requests) == calls
    runner.run(command, retry_credit_refusal=second["invocation_id"])
    assert [row["attempt_index"] for row in journal.attempts(command.run_id, "review")] == [0, 1, 2]
    assert len(provider.requests) == 5


@pytest.mark.parametrize(
    "status,code,ambiguous,body",
    [
        (429, "rate_limit", False, None),
        (500, "credit_balance_exhausted", False, None),
        (429, "credit_balance_exhausted", True, None),
        (401, "credit_balance_exhausted", False, None),
        (429, "credit_balance_exhausted", False, b"{broken"),
        (
            429,
            "credit_balance_exhausted",
            False,
            b'{"id":"resp_known","error":{"code":"credit_balance_exhausted"}}',
        ),
        (
            429,
            "credit_balance_exhausted",
            False,
            b'{"error":{"code":"rate_limit","code":"credit_balance_exhausted"}}',
        ),
    ],
)
def test_other_or_ambiguous_faults_cannot_authorize_retry(repo, setup_writer, status, code, ambiguous, body):
    command, loader, _ = setup_writer
    provider = CreditProvider(FakeProvider(), status=status, code=code, ambiguous=ambiguous, body=body)
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    target = journal.invocation(command.run_id, "review")
    with pytest.raises(
        RunFailure,
        match="credit_refusal_retry_outcome_unknown" if ambiguous else "credit_refusal_retry_not_eligible",
    ):
        runner.run(command, retry_credit_refusal=target["invocation_id"])
    assert len(provider.requests) == 2
    assert len(journal.attempts(command.run_id, "review")) == 1


@pytest.mark.parametrize("refused_operation", ["role", "anchor_repair"])
def test_credit_retry_preserves_one_semantic_anchor_repair(repo, setup_writer, refused_operation):
    command, loader, _ = setup_writer
    command = command.model_copy(update={"review_anchor_policy": "bounded-selection-v1"})
    provider = CreditProvider(FakeProvider(fault="anchor"), operation=refused_operation)
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure, match="provider_http_429"):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    target = journal.invocation(command.run_id, "review")
    result = runner.run(command, retry_credit_refusal=target["invocation_id"])
    attempts = journal.attempts(command.run_id, "review")
    assert len(attempts) == 3
    assert sum(row["attempt_kind"] == "anchor_repair" for row in attempts) == 1
    assert result["token_usage_or_reservation"] == dict(input=500, output=492)
    assert len(provider.requests) == 5
    assert runner.run(command, retry_credit_refusal=target["invocation_id"]) == result
    assert len(provider.requests) == 5


def test_retry_cannot_reset_input_budget(repo, setup_writer):
    command, loader, _ = setup_writer
    command = command.model_copy(update={"limits": RunLimits(max_total_input_tokens=250)})
    provider = CreditProvider(FakeProvider())
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    target = journal.invocation(command.run_id, "review")
    with pytest.raises(RunFailure, match="run_token_budget_exceeded"):
        runner.run(command, retry_credit_refusal=target["invocation_id"])
    assert len(provider.requests) == 2


def test_retry_cannot_reset_elapsed_limit(repo, setup_writer, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from podgen.runtime.evaluation import service

    command, loader, _ = setup_writer
    provider = CreditProvider(FakeProvider())
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    target = journal.invocation(command.run_id, "review")

    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(days=1)

    monkeypatch.setattr(service, "datetime", Expired)
    with pytest.raises(RunFailure, match="run_elapsed_budget_exceeded"):
        runner.run(command, retry_credit_refusal=target["invocation_id"])
    assert len(journal.attempts(command.run_id, "review")) == 1
    assert len(provider.requests) == 2


def test_lost_retry_acknowledgement_never_resubmits(repo, setup_writer):
    command, loader, _ = setup_writer
    inner = FakeProvider()
    provider = CreditProvider(inner)
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    target = journal.invocation(command.run_id, "review")

    def crash():
        raise KeyboardInterrupt()

    inner.on_submit = crash
    with pytest.raises(KeyboardInterrupt):
        runner.run(command, retry_credit_refusal=target["invocation_id"])
    inner.on_submit = None
    with pytest.raises(RunFailure, match="submission_acknowledgement_unknown"):
        runner.run(command, retry_credit_refusal=target["invocation_id"])
    assert len(provider.requests) == 3
    current = journal.invocation(command.run_id, "review")
    with pytest.raises(RunFailure, match="credit_refusal_retry_outcome_unknown"):
        runner.run(command, retry_credit_refusal=current["invocation_id"])
    assert len(provider.requests) == 3


def test_count_refusal_is_not_a_submit_refusal(repo, setup_writer):
    from podgen.runtime.models.contracts import ProviderFault, WireReply

    command, loader, _ = setup_writer
    provider = FakeProvider()

    def refuse_count(body):
        raise ProviderFault(
            "provider_http_429", reply=WireReply(b'{"error":{"code":"credit_balance_exhausted"}}', 429)
        )

    provider.count = refuse_count
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    target = journal.invocation(command.run_id, "draft")
    with pytest.raises(RunFailure, match="credit_refusal_retry_not_eligible"):
        runner.run(command, retry_credit_refusal=target["invocation_id"])
    assert not provider.requests


def test_wrong_target_does_not_change_run_stop_reason(repo, setup_writer):
    from uuid import uuid4

    command, loader, _ = setup_writer
    provider = CreditProvider(FakeProvider())
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    before = journal.run(command.run_id)
    successful = journal.invocation(command.run_id, "draft")
    for wrong in (uuid4(), successful["invocation_id"]):
        with pytest.raises(RunFailure, match="credit_refusal_retry_not_eligible"):
            runner.run(command, retry_credit_refusal=wrong)
        assert journal.run(command.run_id) == before
        assert len(journal.attempts(command.run_id, "review")) == 1
    assert len(provider.requests) == 2


def test_unknown_explains_reconciliation_and_does_not_change_stop_reason(repo, setup_writer):
    from podgen.runtime.evaluation.credit_recovery import recovery_hint

    command, loader, _ = setup_writer
    provider = CreditProvider(FakeProvider(), ambiguous=True)
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    before = journal.run(command.run_id)
    target = journal.invocation(command.run_id, "review")
    with pytest.raises(RunFailure, match="credit_refusal_retry_outcome_unknown") as failure:
        runner.run(command, retry_credit_refusal=target["invocation_id"])
    assert "неизвестен" in recovery_hint(failure.value.code)
    assert journal.run(command.run_id) == before
    assert len(provider.requests) == 2


def test_retry_rechecks_live_actor_rights(repo, setup_writer):
    from podgen.domain.errors import AccessDenied
    from podgen.persistence.database import query

    command, loader, _ = setup_writer
    provider = CreditProvider(FakeProvider())
    runner = app(repo, loader, provider)
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    target = journal.invocation(command.run_id, "review")
    with repo.database.transaction() as tx:
        query(
            tx,
            "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
            p=command.project_id,
            a=repo.actor_id,
        )
    try:
        with pytest.raises(AccessDenied):
            runner.run(command, retry_credit_refusal=target["invocation_id"])
    finally:
        with repo.database.transaction() as tx:
            query(
                tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=command.project_id, a=repo.actor_id
            )
    assert len(journal.attempts(command.run_id, "review")) == 1
    assert len(provider.requests) == 2
