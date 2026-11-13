"""Product checkpoint/history round trip, including historical replay and wire separation."""

import json
from copy import deepcopy

import httpx2
import pytest

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.source_tool_contract import from_manifest
from podgen.runtime.models import research_history_view as view
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.runtime.models.research_history import ResearchHistory
from podgen.runtime.models.research_tools import ResearchTools
from podgen.writing.research_materials.runtime_tools import definitions
from tests.test_research_runtime import answer, invoke, research_setup
from tests.test_source_tool_runtime import call, final


@pytest.mark.parametrize("legacy", [False, True])
def test_checkpoint_history_is_semantic_for_new_runs_and_exact_for_existing_runs(monkeypatch, legacy):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    contract = journal.snapshot["source_tool_contract"]
    if legacy:
        contract.pop("history_view")
        replacements = {item["name"]: item for item in definitions()}
        contract["definitions"] = [replacements.get(item["name"], item) for item in contract["definitions"]]
        original = journal.request(journal.root)
        original["tools"] = contract["definitions"]
        journal.root["request_record_id"] = journal.store_request(original)
    assert from_manifest(journal.snapshot) == contract
    state = "Полные заметки с E1, S1 и цитатой «вода». " * 400
    sent = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            checkpoint = "runtime_continuation" not in json.dumps(body["input"]) and any(
                item.get("type") == "function_call_output" for item in body["input"]
            )
            return httpx2.Response(200, json={"input_tokens": 20000 if checkpoint else 100})
        sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        if turn == 0:
            return answer(body, turn, [dict(type="reasoning", encrypted_content="secret-wire"), call(1)])
        if turn == 1:
            output = dict(
                type="function_call",
                call_id="checkpoint-wire-id",
                name="save_working_state",
                arguments=json.dumps(dict(state=state, resume_state="Check E1.", receipt_ids=["E1"])),
            )
        elif turn == 2:
            output = dict(
                type="function_call",
                call_id="history-wire-id",
                name="read_work_history",
                arguments=json.dumps(dict(kind="conversation", index=0, start=0, end=None)),
            )
        else:
            return answer(body, turn, [final()])
        return answer(body, turn, [output])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        before = deepcopy(sent)
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        assert sent == before  # recovery never repeats provider work

    history = ResearchHistory(journal, journal.root)
    segment = history.latest("source_context_segment")
    raw = journal.read_blob(None, BlobRef.model_validate(segment["data"]["conversation_blob"])).decode()
    assert "secret-wire" in raw and "checkpoint-wire-id" in raw
    delivered = json.loads(sent[-1]["input"][-1]["output"][0]["text"])["text"]
    assert ("secret-wire" in delivered) is legacy
    proxy = ResearchTools(
        journal, journal.root, corpus, history, lambda: None, history_view=contract.get("history_view")
    )

    def all_pages(kind, index):
        text, cursor = "", 0
        while cursor is not None:
            page = proxy.execute(
                "read_work_history", dict(kind=kind, index=index, start=cursor, end=None), read_id="E99"
            )
            assert len(page["text"]) <= 9600
            text += page["text"]
            cursor = page["next_start"]
        return text

    archived = all_pages("conversation", 0)
    notes = json.loads(all_pages("tool_result", 1))
    assert notes["arguments"]["state"] == state
    if legacy:
        assert archived == raw and "raw_arguments" in notes
    else:
        assert set(notes) == {"action", "arguments", "result"}
        semantic = json.loads(archived)
        assert semantic[-1]["arguments"]["state"] == state
        assert semantic[1]["result"]["passage"]["read_id"] == "E1"
        assert "Full exact source passage" in archived
        for technical in ("secret-wire", "checkpoint-wire-id", "raw_arguments", "turn_index", "call_index"):
            assert technical not in archived


def test_anthropic_messages_and_errors_keep_meaning_without_transport():
    transcript = [
        dict(role="user", content="Complete assignment"),
        dict(
            type="anthropic_assistant",
            content=[
                dict(type="thinking", thinking="private", signature="signed-wire"),
                dict(type="text", text="Check the date."),
                dict(type="tool_use", id="wire-1", name="read_source", input={"source_id": "S2"}),
            ],
        ),
        dict(
            type="function_call_output", call_id="wire-1", output='{"error":"invalid_range"}', is_error=True
        ),
    ]
    semantic = view.conversation(transcript)
    assert semantic == [
        dict(speaker="user", text="Complete assignment"),
        dict(speaker="assistant", text="Check the date."),
        dict(
            action="read_source",
            arguments={"source_id": "S2"},
            result={"error": "invalid_range"},
            failed=True,
        ),
    ]
    malformed = dict(
        name="read_source",
        arguments=None,
        raw_arguments="{broken",
        result={"error": "invalid_source_tool_arguments"},
    )
    assert view.tool_result(malformed)["arguments"] == "{broken"
    assert view.encoded(view.tool_result(malformed)).count("{broken") == 1


def test_unknown_semantic_content_is_not_silently_discarded():
    with pytest.raises(ContractError, match="unsupported_history_content"):
        view.conversation([dict(role="user", content=[dict(type="input_image", image_url="data:...")])])


def test_history_pin_and_definitions_cannot_be_mixed(monkeypatch):
    journal, *_ = research_setup(monkeypatch)
    manifest = deepcopy(journal.snapshot)
    manifest["source_tool_contract"].pop("history_view")
    with pytest.raises(ContractError, match="durable_source_tool_definitions_changed"):
        from_manifest(manifest)
