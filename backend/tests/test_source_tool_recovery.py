"""Recover only a proven refused/unsent second turn; preserve paid dialogue and evidence IDs."""

import json
from types import SimpleNamespace

import httpx
import pytest
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
from podgen.runtime.evaluation.input_budget_recovery import authorize_input_retry
from podgen.runtime.evaluation.source_tool_trace import successful_read_passages
from podgen.runtime.evaluation.usage import attempt_usage
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing.projections import CONTRACT

from tests.test_source_tool_runtime import call, final, setup


class Server:
    def __init__(self, mode):
        self.mode, self.sent = mode, []
        self.failed = False

    def __call__(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"input_tokens": 350 if body["metadata"]["podgen_source_turn"] == "1" else 100}
            )
        self.sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        if turn == 1 and not self.failed and self.mode != "budget":
            self.failed = True
            if self.mode == "unknown":
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            return httpx.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
        return httpx.Response(
            200,
            json=dict(
                id=f"resp_{len(self.sent)}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[call(turn + 1)] if turn < 2 else [final()],
                usage=dict(input_tokens=350 if turn == 1 else 100, output_tokens=10),
            ),
        )


def prepare(monkeypatch, mode):
    journal, profile, limits, corpus = setup(monkeypatch)
    if mode == "budget":
        journal.snapshot.update(
            input_contract=CONTRACT, role_input_budget=dict(visible_bytes=1000000, input_tokens=200)
        )
        journal.records[journal.root["request_record_id"]]["context"] = {"role_input_contract": CONTRACT}
    server = Server(mode)

    # count requests intentionally have no metadata, so infer their turn from preserved input.
    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            body = json.loads(request.content)
            count = 350 if len(body["input"]) == 3 else 100
            return httpx.Response(200, json={"input_tokens": count})
        return server(request)

    return journal, profile, limits, corpus, server, handler


def invoke(gateway, row, profile, limits, totals=None):
    return gateway.invoke(
        row.copy(), profile, limits=limits, totals=totals or dict(input=0, output=0), guard=lambda: None
    )


@pytest.mark.parametrize("mode", ["credit", "budget"])
def test_second_turn_recovery_preserves_paid_history_and_evidence(monkeypatch, mode):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, mode)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure) as failure:
            invoke(gateway, journal.root, profile, limits)
        assert failure.value.invocation_id == journal.root["invocation_id"]
        original = journal.root.copy()
        paid = attempt_usage(journal, original, profile.output_limit)
        assert paid == dict(input=100, output=10)
        before = journal.events(original)

        def authorize():
            if mode == "credit":
                return authorize_credit_retry(
                    journal, original["run_id"], original["invocation_id"], guard=lambda: None
                )
            command = SimpleNamespace(
                run_id=original["run_id"],
                planning_parent_run_id=None,
                plan_mode="fixed-plan",
                review_mode=None,
            )
            return authorize_input_retry(
                journal, command, original["invocation_id"], None, 400, guard=lambda: None
            )

        child = authorize()
        assert authorize()["invocation_id"] == child["invocation_id"]
        assert invoke(gateway, child, profile, limits, paid) == '{"answer":"done"}'
        assert journal.events(original) == before
        assert [int(body["metadata"]["podgen_source_turn"]) for body in server.sent] == (
            [0, 1, 1, 2] if mode == "credit" else [0, 1, 2]
        )
        # The retry starts with the exact saved tool reply and retained OpenAI continuation items.
        retried = server.sent[-2]
        assert json.loads(retried["input"][-1]["output"])["passage"]["read_id"] == "E1"
        assert [options["read_id"] for _, _, options in corpus.calls] == ["E1", "E2"]
        assert [receipt["read_id"] for receipt in successful_read_passages(journal, child)] == ["E1", "E2"]
        used = attempt_usage(journal, journal.roots[child["invocation_id"]], profile.output_limit)
        assert used == dict(input=450, output=20)
        # Resuming the completed model response does not resubmit or execute earlier tools.
        sent = len(server.sent)
        assert (
            invoke(gateway, journal.roots[child["invocation_id"]], profile, limits, paid)
            == '{"answer":"done"}'
        )
        assert len(server.sent) == sent and len(corpus.calls) == 2


def test_unknown_second_turn_still_forbids_explicit_retry(monkeypatch):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "unknown")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        with pytest.raises(RunFailure, match="credit_refusal_retry_outcome_unknown"):
            authorize_credit_retry(
                journal, journal.root["run_id"], journal.root["invocation_id"], guard=lambda: None
            )
    assert len(server.sent) == 2
    assert attempt_usage(journal, journal.root, profile.output_limit) == dict(input=450, output=24586)


def test_credit_retry_at_final_turn_keeps_original_turn_and_call_limits(monkeypatch):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "credit")
    original_handler = handler
    final_refused = False

    def refuse_final(request):
        nonlocal final_refused
        body = json.loads(request.content)
        if (
            request.url.path.endswith("/responses")
            and body["metadata"]["podgen_source_turn"] == "2"
            and not final_refused
        ):
            final_refused = True
            server.sent.append(body)
            return httpx.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
        return original_handler(request)

    with httpx.Client(transport=httpx.MockTransport(refuse_final)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        parent = journal.root.copy()
        child = authorize_credit_retry(journal, parent["run_id"], parent["invocation_id"], guard=lambda: None)
        with pytest.raises(RunFailure):
            invoke(gateway, child, profile, limits, attempt_usage(journal, parent, profile.output_limit))
        child = journal.roots[child["invocation_id"]].copy()
        last = authorize_credit_retry(journal, child["run_id"], child["invocation_id"], guard=lambda: None)
        paid = {
            key: sum(attempt_usage(journal, row, profile.output_limit)[key] for row in (parent, child))
            for key in ("input", "output")
        }
        assert invoke(gateway, last, profile, limits, paid) == '{"answer":"done"}'
    assert [body["metadata"]["podgen_source_turn"] for body in server.sent] == ["0", "1", "1", "2", "2"]
    assert server.sent[-1]["tool_choice"] == "none"
    assert len(corpus.calls) == 2
    assert [receipt["read_id"] for receipt in successful_read_passages(journal, last)] == ["E1", "E2"]
    assert paid == dict(input=450, output=20)


def test_input_allowance_is_not_inherited_by_credit_retry_on_later_turn(monkeypatch):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "budget")
    final_refused = False

    def refuse_final(request):
        nonlocal final_refused
        body = json.loads(request.content)
        if (
            request.url.path.endswith("/responses")
            and body["metadata"]["podgen_source_turn"] == "2"
            and not final_refused
        ):
            final_refused = True
            server.sent.append(body)
            return httpx.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(refuse_final)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        command = SimpleNamespace(
            run_id=journal.root["run_id"],
            planning_parent_run_id=None,
            plan_mode="fixed-plan",
            review_mode=None,
        )
        child = authorize_input_retry(
            journal, command, journal.root["invocation_id"], None, 400, guard=lambda: None
        )
        with pytest.raises(RunFailure, match="provider_http_429"):
            invoke(gateway, child, profile, limits, dict(input=100, output=10))
        child = journal.roots[child["invocation_id"]].copy()
        last = authorize_credit_retry(journal, child["run_id"], child["invocation_id"], guard=lambda: None)
        assert invoke(gateway, last, profile, limits, dict(input=450, output=20)) == '{"answer":"done"}'
    assert [body["metadata"]["podgen_source_turn"] for body in server.sent] == ["0", "1", "2", "2"]
    assert len(corpus.calls) == 2
    measurements = [
        journal.read(SimpleNamespace(record_id=event["record_id"]))["data"]["limits"]["input_tokens"]
        for event in journal.events(last)
        if event["event_kind"] == "role_input_measurement"
    ]
    assert measurements == [200]


def test_next_turn_needs_its_own_explicit_input_allowance(monkeypatch):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "budget")

    def larger_final(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens") and len(body["input"]) == 5:
            return httpx.Response(200, json={"input_tokens": 350})
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(larger_final)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        command = SimpleNamespace(
            run_id=journal.root["run_id"],
            planning_parent_run_id=None,
            plan_mode="fixed-plan",
            review_mode=None,
        )
        child = authorize_input_retry(
            journal, command, journal.root["invocation_id"], None, 400, guard=lambda: None
        )
        with pytest.raises(RunFailure, match="role_input_token_budget_exceeded") as failure:
            invoke(gateway, child, profile, limits, dict(input=100, output=10))
        assert failure.value.invocation_id == child["invocation_id"]
        assert [body["metadata"]["podgen_source_turn"] for body in server.sent] == ["0", "1"]
        assert len(corpus.calls) == 2
        # The old 400-token grant cannot authorize this different final request implicitly.
        last = authorize_input_retry(journal, command, child["invocation_id"], None, 360, guard=lambda: None)
        assert invoke(gateway, last, profile, limits, dict(input=450, output=20)) == '{"answer":"done"}'
    assert [body["metadata"]["podgen_source_turn"] for body in server.sent] == ["0", "1", "2"]
    assert len(corpus.calls) == 2


def test_credit_retry_of_same_turn_keeps_exact_input_allowance(monkeypatch):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "budget")
    refused = False

    def refuse_second(request):
        nonlocal refused
        body = json.loads(request.content)
        if (
            request.url.path.endswith("/responses")
            and body["metadata"]["podgen_source_turn"] == "1"
            and not refused
        ):
            refused = True
            server.sent.append(body)
            return httpx.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(refuse_second)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        command = SimpleNamespace(
            run_id=journal.root["run_id"],
            planning_parent_run_id=None,
            plan_mode="fixed-plan",
            review_mode=None,
        )
        child = authorize_input_retry(
            journal, command, journal.root["invocation_id"], None, 400, guard=lambda: None
        )
        with pytest.raises(RunFailure, match="provider_http_429"):
            invoke(gateway, child, profile, limits, dict(input=100, output=10))
        last = authorize_credit_retry(journal, child["run_id"], child["invocation_id"], guard=lambda: None)
        assert invoke(gateway, last, profile, limits, dict(input=100, output=10)) == '{"answer":"done"}'
    assert [body["metadata"]["podgen_source_turn"] for body in server.sent] == ["0", "1", "1", "2"]
    measurements = [
        journal.read(SimpleNamespace(record_id=event["record_id"]))["data"]["limits"]["input_tokens"]
        for event in journal.events(last)
        if event["event_kind"] == "role_input_measurement"
    ]
    assert measurements == [400, 200]
    assert len(corpus.calls) == 2
