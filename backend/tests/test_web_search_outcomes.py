"""Mixed native search outcomes survive the ordinary response, retry and export paths."""

import json

import httpx2
import pytest

from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
from podgen.runtime.evaluation.source_tool_contract import bind
from podgen.runtime.evaluation.source_tool_trace import successful_read_passages
from podgen.runtime.models import web_search
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from tests.test_export_source_turn_usage import export_saved
from tests.test_fact_quick_search import search
from tests.test_research_runtime import answer, invoke, research_setup
from tests.test_source_tool_runtime import call, final


def setup_role(monkeypatch, *, legacy=False):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    contract = journal.snapshot["source_tool_contract"]
    if legacy:
        contract.pop(web_search.KEY)
    body = bind(journal.request(journal.root), "fact-review", profile, contract)
    journal.root["request_record_id"] = journal.store_request(body)
    return journal, profile, limits, corpus


@pytest.mark.parametrize("legacy", [False, True])
def test_mixed_search_with_final_survives_replay_and_export(monkeypatch, tmp_path, legacy):
    journal, profile, limits, corpus = setup_role(monkeypatch, legacy=legacy)
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        failed = dict(search(), id="ws_2", status="failed")
        return answer(json.loads(request.content), 0, [search(), failed, final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        if legacy:
            with pytest.raises(RunFailure, match="provider_web_search_incomplete_or_invalid"):
                invoke(gateway, journal.root, profile, limits)
        else:
            assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
            journal.root["status"] = "tool_running"
            assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
    assert len(requests) == 2  # one count and one generation, including replay
    result = export_saved(journal, tmp_path, monkeypatch)
    usage = result["web_search"]
    assert usage["tool_calls"] == usage["search_calls"] == 1
    assert result["input_tokens"] == 100 and usage["amount_usd"] is None
    assert successful_read_passages(journal, journal.root) == []
    assert usage["unsuccessful_tool_calls"] == 1
    if not legacy:
        entries = json.loads((tmp_path / "archive/fact-review/web-search.json").read_text())
        assert len(entries) == 1
        assert [(r["call_id"], r["status"]) for r in entries[0]["data"]["calls"]] == [
            ("ws_1", "completed"),
            ("ws_2", "failed"),
        ]


@pytest.mark.parametrize("local_read", [False, True])
def test_search_failure_reaches_next_turn_and_survives_credit_retry(monkeypatch, tmp_path, local_read):
    journal, profile, limits, corpus = setup_role(monkeypatch)
    sent = []

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        body = json.loads(request.content)
        turn = int(body["metadata"]["podgen_source_turn"])
        sent.append(turn)
        if turn == 0:
            failed = dict(type="web_search_call", id="ws_2", status="incomplete")
            if local_read:
                failed["action"] = dict(type="search", query="second query")
            return answer(body, turn, [search(), failed] + ([call(1)] if local_read else []))
        assert any(
            x.get("role") == "developer" and "web action failed" in x["content"] for x in body["input"]
        )
        assert (
            any(i.get("id") == "ws_2" and i["action"]["query"] == "second query" for i in body["input"])
            == local_read
        )
        assert any(i.get("type") == "web_search_call" and i["id"] == "ws_1" for i in body["input"])
        if len(sent) == 2:
            return httpx2.Response(429, json=dict(error=dict(code="credit_balance_exhausted")))
        return answer(body, turn, [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="provider_http_429"):
            invoke(gateway, journal.root, profile, limits)
        retry = authorize_credit_retry(
            journal, journal.root["run_id"], journal.root["invocation_id"], guard=lambda: None
        )
        assert (
            invoke(gateway, retry, profile, limits, totals=dict(input=100, output=10)) == '{"answer":"done"}'
        )
    assert sent == [0, 1, 1]
    assert len(successful_read_passages(journal, retry)) == int(local_read)
    usage = export_saved(journal, tmp_path, monkeypatch)["web_search"]
    assert usage["search_calls"] == usage["unsuccessful_tool_calls"] == 1 and not usage["uncertain_turns"]
