"""Instruction propagation and frozen replay; these tests do not judge audio truth."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen.audio.fidelity.adjudication import gemini, openai, realtime
from podgen.audio.fidelity.adjudication.contracts import WindowResolution
from podgen.runtime.evaluation.audio.adjudication import prepare as preparation
from podgen.runtime.evaluation.audio.adjudication.contracts import (
    OpenAIReviewerProfile,
    RealtimeReviewerProfile,
    ReviewConfig,
    ReviewerProfile,
)
from podgen.runtime.evaluation.audio.adjudication.service import reprocess, run
from tests.audio_adjudication_doubles import Routed, setup
from tests.audio_cases import signal
from tests.audio_realtime_cases import configuration as realtime_config
from tests.test_audio_adjudication import decision
from tests.test_audio_adjudication_runtime import Reviewer
from tests.test_audio_realtime_runtime import routed


def legacy_schema():
    path = (
        Path(__file__).resolve().parents[2]
        / "docs/podcast-production/audio/fidelity-adjudication-response.schema.json"
    )
    return json.loads(path.read_text())


def without_descriptions(value):
    if isinstance(value, dict):
        return {k: without_descriptions(v) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [without_descriptions(v) for v in value]
    return value


def test_descriptions_only_change_instructions_not_validation_shape():
    schema = WindowResolution.model_json_schema()
    assert without_descriptions(schema) == legacy_schema()
    fields = schema["$defs"]["CaseResolution"]["properties"]
    for name in ("status", "reason_code", "heard_spans", "explanation"):
        assert "description" in fields[name]
    assert "expected_words" in fields["status"]["description"]
    assert "expected_context" in fields["status"]["description"]
    assert "regardless of ASR differences" in fields["status"]["description"]
    assert "not merely from ASR" in fields["reason_code"]["description"]
    assert "placeholders" in schema["$defs"]["HeardSpan"]["properties"]["words"]["description"]


def test_all_model_facing_schemas_share_the_same_verdict_instructions():
    prompt = "Exact supplied role.\n"
    payload = dict(
        output_language="ru", cases=[dict(case_id="C1", expected_words="1970", asr_transcript_excerpt="1971")]
    )
    schema = WindowResolution.model_json_schema()
    request, gemini_wire = gemini.prepare(prompt, payload, signal(), ReviewerProfile())
    google = json.loads(gemini_wire)
    assert google["response_format"]["schema"] == schema and google["system_instruction"] == prompt
    assert json.loads(google["input"][0]["text"]) == payload
    assert request["adapter_version"] == "gemini-actual-audio-fidelity-review/2"
    request, openai_wire = openai.prepare(prompt, payload, signal(), OpenAIReviewerProfile())
    chat = json.loads(openai_wire)
    assert json.loads(chat["messages"][1]["content"].split("\n", 1)[1]) == schema
    assert chat["messages"][0]["content"] == prompt
    assert json.loads(chat["messages"][2]["content"][0]["text"]) == payload
    assert request["adapter_version"] == "openai-actual-audio-fidelity-review/2"
    request, realtime_wire = realtime.prepare(prompt, payload, signal(), RealtimeReviewerProfile())
    events = json.loads(realtime_wire)
    assert events[0]["session"]["tools"][0]["parameters"] == schema
    assert (
        events[0]["session"]["instructions"]
        == prompt + "\n\nReturn the results by calling submit_audio_review."
    )
    assert json.loads(events[1]["item"]["content"][0]["text"]) == payload
    assert request["adapter_version"] == "openai-realtime-actual-audio-review/2"


def test_validation_does_not_infer_acoustic_verdict_or_blacklist_words():
    row = decision("C1", "confirmed", "audible_mismatch")
    row["heard_spans"][0]["words"] = "audio unavailable"
    row["explanation"] = "Opaque fixture explanation."
    parsed = WindowResolution.model_validate(dict(results=[row])).validate_window(["C1"], 5)
    assert parsed.results[0].heard_spans[0].words == "audio unavailable"


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_prepared_wire_prompt_and_schema_survive_resume(monkeypatch, tmp_path, version):
    journal, config = setup(monkeypatch)
    if version == 1:
        raw = config.model_dump(mode="json")
        raw.pop("fallback")
        raw["contract"] = "audio-fidelity-adjudication/1"
        config = ReviewConfig.model_validate(raw)
    elif version == 3:
        config = realtime_config(config)
    prompt_path = tmp_path / "role.md"
    prompt_path.write_text("Frozen legacy role.\n")
    config = config.model_copy(
        update=dict(
            prompt_path=str(prompt_path), prompt_sha256=hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        )
    )
    with monkeypatch.context() as legacy:
        legacy.setattr(WindowResolution, "model_json_schema", classmethod(lambda cls: legacy_schema()))
        for port in (gemini, openai, realtime):
            legacy.setattr(port, "VERSION", port.VERSION.rsplit("/", 1)[0] + "/1")
        manifest = preparation.prepare(journal, config)
    original_manifest = deepcopy(manifest)
    original_windows = {w["key"]: journal.get(w["key"]) for w in manifest["windows"]}
    assert json.loads(journal.bytes("manifest", manifest["response_schema_blob"])) == legacy_schema()
    prompt_path.write_text("A later role must not rebuild any saved request.\n")

    def forbidden(*args, **kwargs):
        pytest.fail("existing manifest must replay frozen requests")

    monkeypatch.setattr(preparation, "request_for", forbidden)
    monkeypatch.setattr(preparation, "fallback_request", forbidden)
    assert preparation.prepare(journal, config) == original_manifest
    if version == 3:
        transport, client, primary, secondary = routed(config)
    else:
        transport, client = (Reviewer() if version == 1 else Routed()), None
    try:
        run(journal, config, transport)
        run(journal, config, transport)
        reprocess(journal, config, "Replay old receipts with unchanged structural validation")
    finally:
        if client:
            client.close()
    assert journal.get("audio-review:" + str(config.review_id) + ":manifest") == original_manifest
    assert all(journal.get(key) == value for key, value in original_windows.items())
    if version == 3:
        assert len(primary) == 1 and len(secondary) == 2
    else:
        assert len(transport.calls) == (2 if version == 1 else 3)
