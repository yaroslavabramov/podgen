"""Measured admission, overflow recovery and the immutable legacy cutoff."""

import json
from types import SimpleNamespace

import httpx2
import pytest

from podgen.domain.contracts.common import BlobRef
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
from podgen.runtime.evaluation.source_tool_contract import bind
from podgen.runtime.models import research_window
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.runtime.models.research_history import ResearchHistory
from tests.test_research_runtime import answer, invoke, research_setup
from tests.test_source_tool_runtime import call, final


def test_work_and_checkpoint_reservations_use_their_respective_windows():
    profile = SimpleNamespace(context_limit=200000, output_limit=24576)
    body = {"tools": [{"type": "web_search"}]}
    check = research_window.needs_checkpoint
    assert not check(body, profile, 40447, None)
    assert check(body, profile, 40448, None)
    assert not check(body, profile, 103424, research_window.MEASURED)
    assert check(body, profile, 103425, research_window.MEASURED)
    # Both responses now use the same model window.
    profile.context_limit = 128000
    assert not check(body, profile, 78848, research_window.MEASURED)
    assert check(body, profile, 78849, research_window.MEASURED)


@pytest.mark.parametrize(("oversized", "retry"), [(False, False), (True, False), (True, True)])
def test_actual_tool_result_fits_or_remains_reopenable_after_checkpoint(monkeypatch, oversized, retry):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    profile.context_limit = 200000
    body = bind(
        journal.request(journal.root), "fact-review", profile, journal.snapshot["source_tool_contract"]
    )
    journal.root["request_record_id"] = journal.store_request(body)
    sent = []

    def handler(request):
        payload = json.loads(request.content)
        outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
        if request.url.path.endswith("/input_tokens"):
            size = 100 if not outputs else (190000 if oversized else 42000)
            return httpx2.Response(200, json={"input_tokens": size})
        sent.append(payload)
        turn = int(payload["metadata"]["podgen_source_turn"])
        if turn == 0:
            return answer(payload, turn, [call(1)])
        if oversized and turn == 1:
            assert [t.get("name") for t in payload["tools"]] == ["save_working_state"]
            assert not outputs and "pending" in payload["input"][-1]["content"][0]["text"]
            if retry and len(sent) == 2:
                return httpx2.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
            state = dict(
                state="Check the deferred source.",
                resume_state="Read the archived exchange.",
                receipt_ids=["E1"],
            )
            return answer(
                payload,
                turn,
                [
                    dict(
                        type="function_call",
                        status="completed",
                        call_id="save",
                        name="save_working_state",
                        arguments=json.dumps(state),
                    )
                ],
            )
        assert len(payload["tools"]) > 1
        assert bool(outputs) != oversized
        return answer(payload, turn, [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        root = journal.root
        if retry:
            with pytest.raises(RunFailure, match="provider_http_429"):
                invoke(gateway, root, profile, limits)
            root = authorize_credit_retry(journal, root["run_id"], root["invocation_id"], guard=lambda: None)
        assert invoke(gateway, root, profile, limits) == '{"answer":"done"}'
        initial_calls = len(sent)
        root["status"] = "tool_running"
        assert invoke(gateway, root, profile, limits) == '{"answer":"done"}'
        assert len(sent) == initial_calls == 2 + int(oversized) + int(retry)
    if oversized:
        history = ResearchHistory(journal, root)
        segment = history.latest("source_context_segment")
        ref = history.references["source_context_segment"][-1]
        saved = json.loads(
            journal.read_blob(ref, BlobRef.model_validate(segment["data"]["conversation_blob"]))
        )
        assert any(item.get("type") == "function_call_output" and "E1" in item["output"] for item in saved)
