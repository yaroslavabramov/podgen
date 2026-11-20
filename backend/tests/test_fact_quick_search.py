"""Actual SDK wire, counted recovery and exact script binding; no paid network."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.research.materials.targeted import compact, parse
from podgen.runtime.evaluation.source_tool_contract import bind, from_manifest, role_definitions
from podgen.runtime.evaluation.source_tool_trace import successful_read_passages
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.runtime.models.research_binding import bind as bind_research
from podgen.runtime.models.research_continuation import structured_call
from podgen.runtime.models.tool_gateway import _calls
from tests.test_research_runtime import answer, invoke, research_setup
from tests.test_source_tool_runtime import call, final


def search():
    return dict(
        type="web_search_call",
        id="ws_1",
        status="completed",
        action=dict(
            type="search",
            queries=["original dated record"],
            sources=[dict(type="url", url="https://example.org/original")],
        ),
    )


def test_only_fact_openai_gets_native_search_and_count_includes_it(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    contract = from_manifest(journal.snapshot)
    for stage in (
        "plan",
        "plan-review",
        "draft",
        "revise-1",
        "reader-review",
        "fact-review",
        "fact-review-2",
    ):
        for provider in ("openai", "anthropic"):
            tools = role_definitions(contract, stage, provider)
            assert any(t["type"] == "web_search" for t in tools) == (
                stage.startswith("fact-review") and provider == "openai"
            )
    body = bind(journal.request(journal.root), "fact-review", profile, contract)
    assert "web_search_call.action.sources" in body["include"]
    body["metadata"]["podgen_stage"] = "fact-review"
    journal.root["stage"] = "fact-review"
    journal.root["request_record_id"] = journal.store_request(body)
    requests = []

    def handler(request):
        value = json.loads(request.content)
        requests.append((request.url.path, value))
        assert value["tools"][-1] == dict(type="web_search", search_context_size="low")
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        turn = int(value["metadata"]["podgen_source_turn"])
        if turn == 0:
            return answer(value, turn, [search(), call(1)])
        assert any(item.get("type") == "web_search_call" for item in value["input"])
        return answer(value, turn, [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )

        class Interrupted(Exception):
            pass

        def guard():
            if corpus.calls:
                raise Interrupted()

        with pytest.raises(Interrupted):
            invoke(gateway, journal.root, profile, limits, guard=guard)
        assert len(requests) == 2
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        count = len(requests)
        journal.root["status"] = "tool_running"
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        assert len(requests) == count
    events = [
        journal.records[e["record_id"]]["data"]
        for e in journal.receipts
        if e["event_kind"] == "source_web_search"
    ]
    assert len(events) == 1 and events[0]["search_calls"] == 1
    assert events[0]["relationship"] == "navigation_not_source_evidence"
    assert [r["read_id"] for r in successful_read_passages(journal, journal.root)] == ["E1"]
    assert sum(path.endswith("/input_tokens") for path, _ in requests) == 2
    assert len(requests) == 4


def test_undeclared_native_search_is_rejected():
    with pytest.raises(ContractError, match="unexpected_provider_tool_or_output"):
        _calls(dict(output=[search()]))


def test_both_research_paths_keep_exact_words_and_original_neighbors():
    script = dict(
        utterances=[
            dict(speaker="A", text=t)
            for t in ["Сосед слева.", "На письме согласный один и тот же.", "Сосед справа."]
        ]
    )
    request_id, script_id = uuid4(), uuid4()
    ref = RuntimeRef(record_kind="tool_execution", record_id=script_id).model_dump(mode="json")
    records = {
        request_id: dict(context=dict(script_ref=ref, script_hash=digest(script))),
        script_id: dict(output=script),
    }
    journal = SimpleNamespace(read=lambda r: records[r.record_id])
    root = dict(request_record_id=request_id)
    args = dict(
        question="Верно ли объяснение?",
        reason="Проверить смысл.",
        anchors=[dict(utterance_index=1, quote="согласный один и тот же")],
    )
    direct = bind_research(journal, root, args)
    structured = structured_call(dict(args, why_needed=args["reason"]), 0, 0, anchored=True)
    assert bind_research(journal, root, json.loads(structured["arguments"])) == direct
    assert direct["script_context"]["utterances"][1]["text"] == script["utterances"][1]["text"]
    assert len(direct["script_context"]["utterances"]) == 3
    bad = deepcopy(args)
    bad["anchors"][0]["quote"] = "на письме один согласный"
    with pytest.raises(ContractError, match="anchor_mismatch"):
        bind_research(journal, root, bad)
    with pytest.raises(ContractError, match="anchors_required"):
        bind_research(journal, root, dict(args, anchors=[]))


def test_compact_research_rejects_invented_or_oversized_quotes():
    value = dict(
        answer="May.",
        limitations="",
        evidence=[dict(url="https://example.org/doc", quote="dated 12 May", relation="The date.")],
    )
    source = SimpleNamespace(source_id="S7", text="Record, dated 12 May, retained exactly.")
    result = compact(parse(json.dumps(value)), [(value["evidence"][0]["url"], source)])
    assert result["excerpts"][0]["start"] == 8
    assert result["excerpts"][0]["quote"] == "dated 12 May"
    assert "read_source" in result["next_action"]
    value["evidence"][0]["quote"] = "dated 13 May"
    with pytest.raises(ContractError, match="quote_not_in_source"):
        compact(parse(json.dumps(value)), [(value["evidence"][0]["url"], source)])
    value["answer"] = "x" * 2001
    with pytest.raises(ContractError, match="invalid_answer"):
        parse(json.dumps(value))


def test_native_search_and_final_message_are_read_and_recovered(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    contract = journal.snapshot["source_tool_contract"]
    journal.root["stage"] = "fact-review"
    body = bind(journal.request(journal.root), "fact-review", profile, contract)
    journal.root["request_record_id"] = journal.store_request(body)
    sent = []

    def handler(request):
        value = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        sent.append(value)
        return answer(value, 0, [search(), final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        journal.root["status"] = "tool_running"
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
    assert len(sent) == 1
    assert successful_read_passages(journal, journal.root) == []


def test_search_window_rejects_oversized_request_before_generation(monkeypatch):
    from podgen.runtime.evaluation.contracts import RunFailure

    journal, profile, limits, corpus = research_setup(monkeypatch)
    profile = SimpleNamespace(**(vars(profile) | dict(context_limit=200000, output_limit=1000)))
    journal.root["stage"] = "fact-review"
    body = bind(
        journal.request(journal.root), "fact-review", profile, journal.snapshot["source_tool_contract"]
    )
    journal.root["request_record_id"] = journal.store_request(body)
    submitted = []

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=128001))
        submitted.append(request)
        raise AssertionError("Search context exceeded before generation")

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="research_context_checkpoint_unavailable"):
            invoke(gateway, journal.root, profile, limits)
    assert submitted == []


@pytest.mark.parametrize("mode", ["credit", "unknown"])
def test_search_accounting_and_recovery_preserve_paid_search(monkeypatch, tmp_path, mode):
    from podgen.runtime.evaluation.contracts import RunFailure
    from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
    from tests.test_export_source_turn_usage import export_saved

    journal, profile, limits, corpus = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    body = bind(
        journal.request(journal.root), "fact-review", profile, journal.snapshot["source_tool_contract"]
    )
    journal.root["request_record_id"] = journal.store_request(body)
    sent, failed = [], False

    def handler(request):
        nonlocal failed
        value = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        turn = int(value["metadata"]["podgen_source_turn"])
        sent.append(turn)
        if turn == 0:
            return answer(value, turn, [search(), call(1)])
        if not failed:
            failed = True
            if mode == "credit":
                return httpx2.Response(429, json=dict(error=dict(code="credit_balance_exhausted")))
            raise httpx2.ReadTimeout("Unknown response", request=request)
        return answer(value, turn, [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        before = export_saved(journal, tmp_path / "before", monkeypatch)
        assert before["web_search"]["search_calls"] == 1
        assert before["web_search"]["tool_calls"] == 1
        assert before["web_search"]["amount_usd"] is None
        assert bool(before["web_search"]["uncertain_turns"]) == (mode == "unknown")
        if mode == "credit":
            retry = authorize_credit_retry(
                journal, journal.root["run_id"], journal.root["invocation_id"], guard=lambda: None
            )
            assert (
                invoke(gateway, retry, profile, limits, totals=dict(input=100, output=10))
                == '{"answer":"done"}'
            )
            after = export_saved(journal, tmp_path / "after", monkeypatch)
            assert after["web_search"]["search_calls"] == 1
            assert sent == [0, 1, 1]
        else:
            with pytest.raises(RunFailure):
                invoke(gateway, journal.root, profile, limits)
            assert sent == [0, 1]
    assert len(corpus.calls) == 1


def test_completed_search_is_accounted_even_when_other_output_is_invalid(monkeypatch, tmp_path):
    from podgen.runtime.evaluation.contracts import RunFailure
    from tests.test_export_source_turn_usage import export_saved

    journal, profile, limits, corpus = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    body = bind(
        journal.request(journal.root), "fact-review", profile, journal.snapshot["source_tool_contract"]
    )
    journal.root["request_record_id"] = journal.store_request(body)

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        return answer(json.loads(request.content), 0, [search(), dict(type="unexpected_tool")])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
    result = export_saved(journal, tmp_path / "export", monkeypatch)
    assert result["web_search"]["search_calls"] == 1
    assert result["input_tokens"] == 100


def test_manifest_freezes_effective_fact_declarations(monkeypatch):
    journal, _, _, _ = research_setup(monkeypatch)
    baseline = deepcopy(journal.snapshot)
    assert (
        from_manifest(baseline)["fact_request_research"]["parameters"]["properties"]["anchors"]["minItems"]
        == 1
    )
    baseline["source_tool_contract"]["web_search_definition"]["search_context_size"] = "high"
    with pytest.raises(ContractError, match="fact_search_tool_definitions_changed"):
        from_manifest(baseline)
