import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.source_tool_contract import create, from_manifest
from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider

from tests.source_tool_runtime_support import MemoryJournal, install_memory_turns


class Corpus:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments, **options):
        self.calls.append((name, arguments, options))
        return {"passage": {"read_id": options["read_id"], "text": "Full exact source passage"}}


def setup(monkeypatch, provider="openai", turns=3):
    install_memory_turns(monkeypatch)
    contract = create(SimpleNamespace(name="production/1"))
    contract["max_model_turns"] = turns
    body = dict(
        model="test-model",
        instructions="Read sources.",
        input=[dict(role="user", content=[dict(type="input_text", text="Episode")])],
        tools=contract["definitions"],
        tool_choice="auto",
        parallel_tool_calls=False,
        metadata={"podgen_invocation_id": "root"},
        max_output_tokens=24576,
        reasoning={"effort": "high"},
        text={
            "format": {
                "type": "json_schema",
                "name": "answer",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            }
        },
        store=False,
    )
    if provider == "openai":
        body["include"] = ["reasoning.encrypted_content"]
    journal = MemoryJournal({"behavior_profile": "production/1", "source_tool_contract": contract}, body)
    profile = SimpleNamespace(
        provider=provider,
        output_limit=24576,
        context_limit=100000,
        generation_parameters={"store": False},
        timeout_policy={},
    )
    limits = SimpleNamespace(max_total_input_tokens=1000000, max_total_output_tokens=1000000)
    return journal, profile, limits, Corpus()


def call(number):
    return dict(
        type="function_call",
        call_id=f"call_{number}",
        name="read_source",
        status="completed",
        arguments=json.dumps(dict(source_id="S1", start=0, end=4000)),
    )


def final():
    return dict(
        type="message",
        role="assistant",
        status="completed",
        content=[dict(type="output_text", text='{"answer":"done"}')],
    )


def test_openai_real_wire_final_turn_resume_usage_and_receipt_cache(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch)
    requests = []
    reasoning = dict(type="reasoning", id="rs_1", summary=[], encrypted_content="opaque-signed-value")

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(body)
        number = len(requests)
        assert body["max_output_tokens"] == 24576 and body["tools"]
        assert body["include"] == ["reasoning.encrypted_content"]
        if number == 2:
            assert body["input"][1] == reasoning
            assert json.loads(body["input"][-1]["output"])["passage"]["text"] == "Full exact source passage"
        if number == 3:
            assert body["tool_choice"] == "none"
        return httpx.Response(
            200,
            json=dict(
                id=f"resp_{number}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[reasoning, call(number)] if number < 3 else [final()],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        assert (
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=lambda: None,
            )
            == '{"answer":"done"}'
        )
        assert journal.root["input_tokens"] == 300 and journal.root["output_tokens"] == 30
        assert len(corpus.calls) == 2 and corpus.calls[0][2]["max_read_chars"] == 9600
        assert len({body["metadata"]["podgen_invocation_id"] for body in requests}) == 3
        reads = journal.read_count
        assert (
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=lambda: None,
            )
            == '{"answer":"done"}'
        )
        assert len(requests) == 3 and len(corpus.calls) == 2
        # Resume loads each immutable receipt once, instead of once per loop and tool call.
        assert journal.read_count - reads < 30
        assert model_turns.usage(journal, journal.root, profile.output_limit)["output"] == 30


def test_anthropic_real_wire_preserves_signed_blocks_and_native_results(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch, "anthropic", turns=2)
    signed = [
        dict(type="thinking", thinking="Private reasoning", signature="signature"),
        dict(type="redacted_thinking", data="opaque"),
        dict(type="tool_use", id="tool_1", name="read_source", input=dict(source_id="S1", start=0, end=4000)),
    ]
    submitted = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        submitted.append(body)
        if len(submitted) == 2:
            assert body["messages"][1] == dict(role="assistant", content=signed)
            assert body["messages"][2]["content"][0]["type"] == "tool_result"
            assert body["tool_choice"] == {"type": "none"}
            assert body["output_config"] == submitted[0]["output_config"]
        return httpx.Response(
            200,
            json=dict(
                type="message",
                role="assistant",
                model=body["model"],
                id=f"msg_{len(submitted)}",
                content=signed if len(submitted) == 1 else [dict(type="text", text='{"answer":"done"}')],
                stop_reason="tool_use" if len(submitted) == 1 else "end_turn",
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: AnthropicProvider("test-secret", http_client=client), source_corpus=corpus
        )
        assert (
            gateway.invoke(
                journal.root, profile, limits=limits, totals=dict(input=0, output=0), guard=lambda: None
            )
            == '{"answer":"done"}'
        )
    assert journal.root["input_tokens"] == 200 and journal.root["output_tokens"] == 20


def test_unknown_submission_never_retries_and_reserves_output(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch)
    sent = []

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        sent.append(request)
        raise httpx.ReadTimeout("lost acknowledgement", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        for _ in range(2):
            with pytest.raises(RunFailure):
                gateway.invoke(
                    journal.root.copy(),
                    profile,
                    limits=limits,
                    totals=dict(input=0, output=0),
                    guard=lambda: None,
                )
    assert len(sent) == 1 and journal.root["status"] == "outcome_unknown"
    assert model_turns.usage(journal, journal.root, profile.output_limit) == dict(
        input=100, output=24576, known_output=0, unknown=True
    )


def test_tool_pin_versions_keep_old_read_and_final_turn_policy():
    current = create(SimpleNamespace(name="production/1"))
    for version in (
        "frozen-corpus-tools/1",
        "frozen-corpus-tools/2",
        "frozen-corpus-tools/3",
        "frozen-corpus-tools/4",
    ):
        value = deepcopy(current)
        value["version"] = version
        if not version.endswith("/4"):
            value.pop("retrieval")
        if version.endswith(("/1", "/2")):
            value.pop("max_read_chars")
        if version.endswith("/1"):
            value.pop("final_turn")
        assert from_manifest(dict(behavior_profile="production/1", source_tool_contract=value)) == value
    current["max_read_chars"] = True
    with pytest.raises(ContractError):
        from_manifest(dict(behavior_profile="production/1", source_tool_contract=current))


def test_resume_counted_turn_does_not_charge_it_twice(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch, turns=2)
    limits.max_total_input_tokens = 200
    counts, sent = [], []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            counts.append(body)
            return httpx.Response(200, json={"input_tokens": 100})
        sent.append(body)
        return httpx.Response(
            200,
            json=dict(
                id=f"resp_{len(sent)}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[call(1)] if len(sent) == 1 else [final()],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    class Interrupted(Exception):
        pass

    def guard():
        if len(counts) == 2:
            raise Interrupted()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(Interrupted):
            gateway.invoke(
                journal.root.copy(), profile, limits=limits, totals=dict(input=0, output=0), guard=guard
            )
        assert len(sent) == 1 and journal.turns[1]["status"] == "counted"
        assert (
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=lambda: None,
            )
            == '{"answer":"done"}'
        )
    assert len(counts) == 2 and len(sent) == 2 and len(corpus.calls) == 1
    assert journal.root["input_tokens"] == 200


def test_budget_blocks_later_turn_before_submission_and_excludes_unsent_usage(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch, turns=2)
    limits.max_total_input_tokens = 199
    sent = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        sent.append(body)
        return httpx.Response(
            200,
            json=dict(
                id="resp_first",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[call(1)],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="run_token_budget_exceeded"):
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=lambda: None,
            )
    assert len(sent) == 1
    assert model_turns.usage(journal, journal.root, profile.output_limit) == dict(
        input=100, output=10, known_output=10, unknown=False
    )


@pytest.mark.parametrize(
    "output,code",
    [
        ([dict(type="message", role="assistant", content="broken")], "provider_malformed_content"),
        ([dict(type="message", role="assistant", content=[])], "provider_output_missing"),
        ([call(1)], "tool_call_after_tools_closed"),
    ],
)
def test_invalid_final_response_fails_root(monkeypatch, output, code):
    journal, profile, limits, corpus = setup(monkeypatch, turns=1)

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(
            200,
            json=dict(
                id="resp_final",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=output,
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match=code):
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=lambda: None,
            )
    assert journal.root["status"] in ("failed", "validation_failed")


@pytest.mark.parametrize("compact_profile", [False, True])
def test_source_search_failure_resumes_unfinished_tool_without_repaying_provider(
    monkeypatch, compact_profile
):
    from podgen.retrieval import RetrievalUnavailable

    journal, profile, limits, corpus = setup(monkeypatch, turns=2)
    if compact_profile:
        from podgen.retrieval.config import COMPACT_PROFILE_V1

        journal.snapshot["source_tool_contract"]["retrieval"] = deepcopy(COMPACT_PROFILE_V1)
    requests = []
    attempts = []

    def execute(name, arguments, **options):
        attempts.append(options["read_id"])
        if len(attempts) == 1:
            raise RetrievalUnavailable("source_search_index_unavailable")
        return {"passage": {"read_id": options["read_id"], "text": "Exact original"}}

    corpus.execute = execute

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(body)
        return httpx.Response(
            200,
            json=dict(
                id=f"resp_{len(requests)}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[call(1)] if len(requests) == 1 else [final()],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="source_search_index_unavailable"):
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=lambda: None,
            )
        assert journal.root["status"] == "tool_running" and len(requests) == 1
        assert (
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=lambda: None,
            )
            == '{"answer":"done"}'
        )
    assert len(requests) == 2 and attempts == ["E1", "E1"]
