"""One exact native measurement per request, fresh access before generation."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import httpx2
import pytest

from podgen.domain.errors import AccessDenied, Conflict
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.runtime.models.research_context import ResearchContext
from podgen.runtime.models.research_history import ResearchHistory
from tests.test_research_runtime import answer, call, final, invoke, research_setup


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_full_reading_cycle_counts_once_and_replays_without_count_or_submit(monkeypatch, provider):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    profile.provider = provider
    journal.snapshot["stages"] = ["draft"]
    native_counts, native_submits = [], []
    transport = httpx2 if provider == "openai" else httpx

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith(("/input_tokens", "/count_tokens")):
            native_counts.append(body)
            return transport.Response(200, json={"input_tokens": 100})
        native_submits.append(body)
        turn = len(native_submits) - 1
        if provider == "openai":
            return answer(body, turn, [call(turn + 1)] if turn < 12 else [final()])
        return transport.Response(
            200,
            json=dict(
                type="message",
                role="assistant",
                model=body["model"],
                id=f"msg_{turn}",
                stop_reason="tool_use" if turn < 12 else "end_turn",
                content=[
                    dict(
                        type="tool_use",
                        id=f"call_{turn + 1}",
                        name="read_source",
                        input=dict(source_id="S1", start=0, end=4000),
                    )
                ]
                if turn < 12
                else [dict(type="text", text='{"answer":"done"}')],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with transport.Client(transport=transport.MockTransport(handler)) as client:
        factory = (
            (lambda _: OpenAIProvider("test-key-absent", http_client=client))
            if provider == "openai"
            else (lambda _: AnthropicProvider("test-key-absent", http_client=client))
        )
        gateway = ModelGateway(journal, factory, source_corpus=corpus)
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        assert len(native_counts) == len(native_submits) == 13
        assert len(corpus.calls) == 12
        kinds = [event["event_kind"] for event in journal.events(journal.root)]
        assert kinds.count("source_context_count") == kinds.count("count_receipt_reused") == 13
        assert "count_response" not in kinds
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        assert len(native_counts) == len(native_submits) == 13


def measured(monkeypatch):
    journal, profile, _, _ = research_setup(monkeypatch)
    counts = []
    provider = SimpleNamespace(count=lambda body: counts.append(body) or WireReply(b'{"input_tokens":100}'))
    gateway = ModelGateway(journal, lambda _: provider)
    body = journal.request(journal.root)
    history = ResearchHistory(journal, journal.root)
    context = ResearchContext(gateway, journal.root, profile, body, history, lambda: None)
    assert context.measured(body, 0, "normal") == 100
    # This test mutates persisted evidence, after its normal combined checkpoint.
    context.pending_phase.commit()
    assert not context.pending_counts
    return journal, profile, body, context, counts


@pytest.mark.parametrize(
    "change", ["input", "schema", "tools", "adapter", "provider", "native_blob", "intent", "response"]
)
def test_count_receipt_rejects_changed_request_provider_or_durable_evidence(monkeypatch, change):
    journal, profile, original, context, counts = measured(monkeypatch)
    receipt = context.receipt(original)
    body = deepcopy(original)
    if change == "input":
        body["input"][0]["content"][0]["text"] += " changed"
    if change == "schema":
        body["text"]["format"]["schema"]["title"] = "Changed schema"
    if change == "tools":
        body["tools"] = body["tools"][:-1]
    if change == "adapter":
        profile.adapter_version = "different-adapter"
    if change == "provider":
        profile.provider = "anthropic"
    if change in {"native_blob", "intent", "response"}:
        record = journal.records[receipt.reference.record_id]
        if change == "native_blob":
            record["data"]["native_request_blob"] = journal.blob(b"{}").model_dump(mode="json")
        if change == "intent":
            record["data"]["provider_configuration"]["adapter_version"] = "tampered"
        if change == "response":
            record["response_blob"] = journal.blob(b'{"input_tokens":101}').model_dump(mode="json")
    with pytest.raises(Conflict):
        receipt.validate(journal, body, profile)
    assert len(counts) == 1


def test_revocation_after_count_prevents_any_generation(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    revoked, submissions = False, []

    def handler(request):
        nonlocal revoked
        if request.url.path.endswith("/input_tokens"):
            revoked = True
            return httpx2.Response(200, json={"input_tokens": 100})
        submissions.append(request)
        pytest.fail("generation after access withdrawal")

    def guard():
        if revoked:
            raise AccessDenied()

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-key-absent", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="reference_unavailable"):
            invoke(gateway, journal.root, profile, limits, guard=guard)
    assert not submissions
