"""Thin indexes retain exact receipts and raw bodies while avoiding graph rereads."""

from copy import deepcopy

from podgen.runtime.models.research_history import ResearchHistory
from tests.test_research_runtime import research_setup


def test_appended_metadata_indexes_are_constant_cost_and_never_cache_full_working_bodies(monkeypatch):
    journal, _, _, _ = research_setup(monkeypatch)
    history = ResearchHistory(journal, journal.root)
    full = "FULL WORKING STATE " * 10000
    for index in range(80):
        for kind, data in (
            (
                "source_context_count",
                dict(
                    request_hash=f"request-{index}", input_tokens=index + 1, turn_index=index, label="normal"
                ),
            ),
            (
                "source_context_plan",
                dict(turn_index=index, conversation_hash=f"conversation-{index}", checkpoint=False),
            ),
            ("submit_intent", dict(source_turn_index=index)),
            ("provider_response", dict(source_turn_index=index, full=full)),
            (
                "source_tool_result",
                dict(
                    turn_index=index,
                    call_index=index,
                    call_id=f"call_{index}",
                    name="read_source",
                    arguments=dict(full=full),
                    result=dict(passage=dict(read_id=f"E{index + 1}", text=full)),
                ),
            ),
        ):
            ref = journal.event(journal.root, kind, data=data)
            history.appended(ref, kind, data=data)
    before = journal.read_count
    for _ in range(5):
        for index in range(80):
            assert history.counted(f"request-{index}").data["input_tokens"] == index + 1
            assert history.context_plan(index).data["conversation_hash"] == f"conversation-{index}"
        assert history.submitted_turns() == frozenset(range(80))
    assert journal.read_count == before
    assert history.receipts == {f"E{i + 1}" for i in range(80)}
    assert "FULL WORKING STATE" not in repr(vars(history))
    raw = history.for_call("source_tool_result", 79)[0]
    assert raw["data"]["result"]["passage"]["text"] == full and journal.read_count == before + 1
    before_response = journal.read_count
    assert history.provider_response(79)["data"]["full"] == full
    assert journal.read_count == before_response + 1
    assert history.provider_response(80) is None
    assert journal.read_count == before_response + 1
    restored = ResearchHistory(journal, journal.root)
    assert restored.counted("request-79").data == history.counted("request-79").data
    assert restored.receipts == history.receipts
    assert restored.for_call("source_tool_result", 79)[0] == raw
    snapshot = deepcopy(journal.records)
    restored.counted("request-79").data["input_tokens"] = -1
    assert journal.records == snapshot  # metadata is not the immutable journal payload
