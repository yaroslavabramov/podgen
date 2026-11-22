"""Explicit total-input recovery resumes a counted physical turn without repaying its prefix."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest

from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.recovery import authorize_recovery
from podgen.runtime.evaluation.source_tool_trace import successful_read_passages
from podgen.runtime.evaluation.total_input_recovery import authorize_total_input_retry, effective_ceiling
from podgen.runtime.evaluation.usage import attempt_usage
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing_cli import parser
from tests.test_source_tool_recovery import invoke, prepare


def case(monkeypatch):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "budget")
    journal.snapshot["role_input_budget"]["input_tokens"] = 1000
    limits.max_total_input_tokens = 199
    journal.snapshot.update(
        stages=["draft"],
        command={
            "limits": dict(
                max_total_input_tokens=199, max_total_output_tokens=1000000, max_elapsed_seconds=7200
            )
        },
    )
    command = SimpleNamespace(run_id=journal.root["run_id"])
    return journal, profile, limits, corpus, server, handler, command


@pytest.mark.parametrize("remaining_limit", [None, "output", "context"])
def test_explicit_ceiling_resumes_exact_unsent_turn_and_preserves_other_limits(monkeypatch, remaining_limit):
    journal, profile, limits, corpus, server, handler, command = case(monkeypatch)
    frozen = deepcopy(journal.snapshot)
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="run_token_budget_exceeded") as error:
            invoke(gateway, journal.root, profile, limits)
        assert error.value.invocation_id == journal.root["invocation_id"]
        paid = attempt_usage(journal, journal.root, profile.output_limit)
        assert paid == dict(input=100, output=10) and len(server.sent) == 1
        events = journal.events(journal.root)
        child = authorize_total_input_retry(
            journal, command, journal.root["invocation_id"], 700, guard=lambda: None
        )
        assert (
            authorize_total_input_retry(
                journal, command, journal.root["invocation_id"], 700, guard=lambda: None
            )["invocation_id"]
            == child["invocation_id"]
        )
        with pytest.raises(Conflict, match="total_input_allowance_changed"):
            authorize_total_input_retry(
                journal, command, journal.root["invocation_id"], 800, guard=lambda: None
            )
        gateway.total_input_ceiling = effective_ceiling(journal, command.run_id)
        assert gateway.total_input_ceiling == 700
        if remaining_limit == "output":
            limits.max_total_output_tokens = 24580
        elif remaining_limit == "context":
            profile.context_limit = 24700
        if remaining_limit is not None:
            with pytest.raises(
                RunFailure,
                match="run_token_budget_exceeded"
                if remaining_limit == "output"
                else "context_capacity_exceeded",
            ):
                invoke(gateway, child, profile, limits, paid)
            assert len(server.sent) == 1
        else:
            assert invoke(gateway, child, profile, limits, paid) == '{"answer":"done"}'
            assert [body["metadata"]["podgen_source_turn"] for body in server.sent] == ["0", "1", "2"]
            assert len(corpus.calls) == 2
            assert [p["read_id"] for p in successful_read_passages(journal, child)] == ["E1", "E2"]
            assert attempt_usage(
                journal, journal.roots[child["invocation_id"]], profile.output_limit
            ) == dict(input=450, output=20)
            assert (
                invoke(gateway, journal.roots[child["invocation_id"]], profile, limits, paid)
                == '{"answer":"done"}'
            )
            assert len(server.sent) == 3
            assert (
                authorize_total_input_retry(
                    journal,
                    command,
                    journal.root["invocation_id"],
                    700,
                    guard=lambda: None,
                    allow_create=False,
                )["invocation_id"]
                == child["invocation_id"]
            )
        assert journal.snapshot == frozen and journal.events(journal.root) == events
        assert journal.request(child)["max_output_tokens"] == 24576
        assert {k: v for k, v in journal.request(child).items() if k != "metadata"} == {
            k: v for k, v in journal.request(journal.turns[1]).items() if k != "metadata"
        }


@pytest.mark.parametrize("ceiling", [True, 198, 199, 449])
def test_ceiling_must_explicitly_cover_proven_total_input_refusal(monkeypatch, ceiling):
    journal, profile, limits, corpus, server, handler, command = case(monkeypatch)
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        with pytest.raises(RunFailure, match="total_input_allowance_invalid"):
            authorize_total_input_retry(
                journal, command, journal.root["invocation_id"], ceiling, guard=lambda: None
            )
    assert len(journal.roots) == 1 and len(server.sent) == 1


def test_local_input_refusal_is_not_a_total_input_authorization(monkeypatch):
    journal, profile, limits, corpus, server, handler, command = case(monkeypatch)
    journal.snapshot["role_input_budget"]["input_tokens"] = 200
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="role_input_token_budget_exceeded"):
            invoke(gateway, journal.root, profile, limits)
        with pytest.raises(RunFailure, match="input_budget_retry_not_eligible"):
            authorize_total_input_retry(
                journal, command, journal.root["invocation_id"], 700, guard=lambda: None
            )
    assert len(journal.roots) == 1


def test_total_input_action_requires_both_target_and_ceiling():
    command = SimpleNamespace(run_id=uuid4())
    for kwargs in (
        {"total_input_target": uuid4()},
        {"max_total_input_tokens": 700},
        {"total_input_target": uuid4(), "max_total_input_tokens": 700, "credit_target": uuid4()},
    ):
        with pytest.raises(ContractError):
            authorize_recovery(None, command, guard=lambda: None, **kwargs)
    args = parser().parse_args(
        [
            "--project",
            str(uuid4()),
            "--actor",
            str(uuid4()),
            "--objects",
            "/tmp/podgen",
            "resume",
            "--run-id",
            str(command.run_id),
            "--export-dir",
            "/tmp/export",
            "--retry-total-input-budget",
            str(uuid4()),
            "--max-total-input-tokens",
            "1310720",
        ]
    )
    assert args.max_total_input_tokens == 1310720 and args.retry_total_input_budget


def test_output_only_refusal_cannot_authorize_total_input_growth(monkeypatch):
    journal, profile, limits, corpus, server, handler, command = case(monkeypatch)
    limits.max_total_input_tokens = 1000
    limits.max_total_output_tokens = 24580
    journal.snapshot["command"]["limits"]["max_total_input_tokens"] = 1000
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="run_token_budget_exceeded"):
            invoke(gateway, journal.root, profile, limits)
        with pytest.raises(RunFailure, match="total_input_allowance_invalid"):
            authorize_total_input_retry(
                journal, command, journal.root["invocation_id"], 2000, guard=lambda: None
            )
    assert len(server.sent) == 1 and len(journal.roots) == 1


def test_total_input_recovery_still_runs_elapsed_guard_before_authorization(monkeypatch):
    journal, profile, limits, corpus, server, handler, command = case(monkeypatch)
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)

        def expired():
            raise RunFailure("run_elapsed_budget_exceeded", run_id=command.run_id)

        with pytest.raises(RunFailure, match="run_elapsed_budget_exceeded"):
            authorize_total_input_retry(journal, command, journal.root["invocation_id"], 700, guard=expired)
    assert len(journal.roots) == 1 and len(server.sent) == 1
