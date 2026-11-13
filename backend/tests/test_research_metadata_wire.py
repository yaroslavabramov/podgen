"""Optimization must not change one byte of native requests, even across recovery."""

import json
from copy import deepcopy

import httpx2
import pytest

from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
from podgen.runtime.evaluation.usage import attempt_usage
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from tests.research_history_support import LinearMetadataHistory
from tests.test_research_runtime import answer, call, final, invoke, research_setup


@pytest.mark.parametrize("scenario", ["ordinary", "checkpoint", "credit", "pending", "paused"])
def test_indexed_and_linear_runtime_send_exactly_the_same_native_bytes(monkeypatch, scenario):
    from podgen.runtime.models.research_history import ResearchHistory
    from podgen.sources.material_requests import MaterialResolution

    original, profile, limits, original_corpus = research_setup(monkeypatch)
    results = []
    for history_type in (LinearMetadataHistory, ResearchHistory):
        monkeypatch.setattr("podgen.runtime.models.research_tool_gateway.ResearchHistory", history_type)
        journal, corpus = deepcopy(original), deepcopy(original_corpus)
        corpus._materials = type("Materials", (), {"runtime_admissions": []})()
        corpus.refresh = lambda: None

        class Resolver:
            pending = scenario == "pending"

            def restore(self, **kwargs):
                return ()

            def resolve(self, *args, **kwargs):
                return MaterialResolution(
                    dict(status="pending", handoff_ref={"saved": True})
                    if self.pending
                    else dict(status="resolved", source_id="S1")
                )

        resolver = Resolver()
        if scenario == "pending":
            corpus.material_resolver = resolver
        wire, refused = [], False

        def handler(request, wire=wire):
            nonlocal refused
            body = json.loads(request.content)
            counting = request.url.path.endswith("/input_tokens")
            wire.append((request.url.path, request.content, request.headers.get("x-client-request-id")))
            if counting:
                large = scenario == "checkpoint" and any(
                    item.get("type") == "function_call_output" for item in body["input"]
                )
                return httpx2.Response(200, json={"input_tokens": 20000 if large else 100})
            turn = int(body["metadata"]["podgen_source_turn"])
            if scenario == "credit" and turn == 2 and not refused:
                refused = True
                return httpx2.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
            if scenario == "pending" and turn == 0:
                output = [
                    dict(
                        type="function_call",
                        call_id="request_doc",
                        name="request_source",
                        arguments=json.dumps(
                            dict(source_id="S1", url=None, reason="Exact missing evidence.")
                        ),
                        status="completed",
                    )
                ]
            elif scenario == "checkpoint" and turn == 1:
                output = [
                    dict(
                        type="function_call",
                        call_id="checkpoint",
                        name="save_working_state",
                        arguments=json.dumps(
                            dict(
                                state="Full state retained. " * 1000,
                                resume_state="Continue from exact E1.",
                                receipt_ids=["E1"],
                            )
                        ),
                        status="completed",
                    )
                ]
            elif turn < (
                3
                if scenario == "credit"
                else 2
                if scenario == "checkpoint"
                else 4
                if scenario == "ordinary"
                else 23
                if scenario == "paused"
                else 1
            ):
                output = [
                    dict(
                        type="reasoning",
                        id=f"rs_{turn}",
                        summary=[],
                        encrypted_content=f"opaque_exact_{turn}",
                    ),
                    call(turn + 1),
                ]
            else:
                output = [final()]
            return answer(body, turn, output)

        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
            gateway = ModelGateway(
                journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
            )
            if scenario == "paused":

                class Paused(Exception):
                    pass

                def guard(corpus=corpus):
                    if len(corpus.calls) == 22:
                        raise Paused()

                with pytest.raises(Paused):
                    invoke(gateway, journal.root, profile, limits, guard=guard)
                assert sum(path.endswith("/responses") for path, _, _ in wire) == 22
            if scenario in {"credit", "pending"}:
                with pytest.raises(RunFailure):
                    invoke(gateway, journal.root, profile, limits)
                if scenario == "credit":
                    parent = journal.root.copy()
                    paid = attempt_usage(journal, parent, profile.output_limit)
                    root = authorize_credit_retry(
                        journal, parent["run_id"], parent["invocation_id"], guard=lambda: None
                    )
                else:
                    resolver.pending = False
                    root, paid = journal.root, None
            else:
                root, paid = journal.root, None
            assert invoke(gateway, root, profile, limits, totals=paid) == '{"answer":"done"}'
            if scenario == "paused":
                submitted = [
                    int(json.loads(body)["metadata"]["podgen_source_turn"])
                    for path, body, _ in wire
                    if path.endswith("/responses")
                ]
                assert submitted == list(range(24))
                assert len(corpus.calls) == 23
            before = len(wire)
            assert (
                invoke(gateway, journal.roots[root["invocation_id"]], profile, limits, totals=paid)
                == '{"answer":"done"}'
            )
            assert len(wire) == before
        results.append(
            (
                wire,
                corpus.calls,
                attempt_usage(journal, journal.roots[root["invocation_id"]], profile.output_limit),
            )
        )
    assert results[0] == results[1]
