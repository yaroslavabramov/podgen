"""Offline checks for full cold inputs, independent desks and spend-safe execution."""

import copy
import json
from pathlib import Path

import pytest
import run
from contracts import ReaderDesks

from podgen.runtime.models.contracts import WireReply
from podgen.writing.roles import WriteOutput

HERE = Path(__file__).resolve().parent


def output():
    return dict(
        language_review=dict(findings=[]),
        reader_review=dict(
            reading_mode="script_only",
            listener_answer="A complete telling.",
            important_limits=[],
            strengths=[],
            findings=[],
            research_requests=[],
            duration_note="No measured audio.",
            recommendation="keep",
            revision_scope=dict(
                scale="none", reason="No required repairs.", anchors=[], dependent_passages=[]
            ),
        ),
    )


def test_all_requests_are_complete_cold_inputs_and_the_original_reader_shape_is_not_reordered():
    frozen = run.read(HERE / "frozen.json")
    for row in frozen["cases"]:
        case = row["case"]
        request = run.read(HERE / "runs" / case / "request.json")
        assert run.sha((HERE / "runs" / case / "request.json").read_bytes()) == row["request_sha256"]
        payload = json.loads(request["input"][0]["content"][0]["text"])
        assert set(payload) == {
            "script",
            "audience",
            "format",
            "genre",
            "output_language",
            "duration_context",
        }
        full = run.read(HERE / "texts" / (case + ".json"))
        assert [
            {k: v for k, v in item.items() if k != "index"} for item in payload["script"]["utterances"]
        ] == full["utterances"]
        assert request["model"] == "gpt-6.1-terra" and request["reasoning"] == {"effort": "medium"}
        schema = request["text"]["format"]["schema"]
        assert list(schema["properties"]) == ["language_review", "reader_review"]
        assert list(schema["$defs"]["ReaderAssessmentV4"]["properties"]) == [
            "reading_mode",
            "listener_answer",
            "important_limits",
            "strengths",
            "findings",
            "research_requests",
            "duration_note",
            "recommendation",
            "revision_scope",
        ]

        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    assert set(value) == {"$ref"}
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(schema)
    prompt = (HERE / "instructions.txt").read_text()
    assert not any(word in prompt.lower() for word in ("snow", "cholera", "court", "verdict", "83"))
    assert (HERE / "listener-instructions.md").read_text() in prompt


def test_language_anchors_validate_exactly_and_cross_desk_consistency_is_reported_not_rewritten():
    script = WriteOutput.model_validate(run.read(HERE / "texts/01.json"))
    raw = output()
    raw["language_review"]["findings"] = [
        dict(
            kind="form",
            priority="important",
            anchors=[dict(utterance_index=0, quote=script.utterances[0].text)],
            observation="A form issue.",
            requested_change="Repair the form.",
            preserve=["Meaning."],
        )
    ]
    parsed = ReaderDesks.model_validate(raw)
    parsed.validate_against(script)
    assert not parsed.consistency()
    assert parsed.reader_review.recommendation == "keep"
    broken = copy.deepcopy(raw)
    broken["language_review"]["findings"][0]["anchors"][0]["quote"] = "not present at all"
    with pytest.raises(ValueError, match="quote_mismatch"):
        ReaderDesks.model_validate(broken).validate_against(script)


def test_real_adapter_reply_is_archived_once_and_replay_needs_no_provider(tmp_path, monkeypatch):
    folder = tmp_path / "runs/01"
    request = run.read(HERE / "runs/01/request.json")
    run.save(folder / "request.json", request)
    run.save(tmp_path / "texts/01.json", run.read(HERE / "texts/01.json"))
    calls = []

    class FakeProvider:
        def __init__(self, key):
            pass

        def count(self, body):
            calls.append("count")
            return WireReply(b'{"input_tokens":100}', 200, "count-id")

        def submit(self, body, client_request_id):
            calls.append("submit")
            envelope = dict(
                model=body["model"],
                metadata=body["metadata"],
                id="resp_test",
                status="completed",
                output=[dict(type="message", content=[dict(type="output_text", text=json.dumps(output()))])],
                usage=dict(input_tokens=100, input_tokens_details={"cached_tokens": 0}, output_tokens=100),
            )
            return WireReply(json.dumps(envelope).encode(), 200, "actual-request-id")

    monkeypatch.setattr(run, "HERE", tmp_path)
    monkeypatch.setattr(run, "OpenAIProvider", FakeProvider)
    first = run.live_case("01")
    assert run.live_case("01") == first
    assert run.replay_case("01") == first
    assert calls == ["count", "submit"]
    assert run.read(folder / "transport.json")["request_id"] == "actual-request-id"
