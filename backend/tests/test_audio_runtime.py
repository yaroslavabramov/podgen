"""Actual PostgreSQL, immutable blob/ACL storage and native approved text; only providers are doubled."""

import json
from copy import deepcopy

import pytest
from podgen import audio_evaluation_cli, writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.audio.contracts import ComparisonConfig
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.evaluation.audio.prepare import prepare
from podgen.runtime.evaluation.reading_cycle import outcome_ref

from tests.audio_cases import config, response, signal
from tests.test_editorial_independence import invoke
from tests.test_independent_readings import ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset


class FakeAudioTransport:
    def __init__(self):
        self.calls = []

    def submit(self, request, credential):
        self.calls.append(request)
        return response(request.provider, signal())


def test_native_approved_runtime_input_cli_lossless_blind_export_and_resume(
    own_dataset, tmp_path, monkeypatch
):
    repo, project, *_ = own_dataset
    prefix, options, script = setup_review(own_dataset, tmp_path, monkeypatch)
    reader = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: reader)
    assert invoke(prefix, "run", **options) == 0
    raw = config(["elevenlabs", "gemini", "inworld", "fish", "minimax"]).model_dump(mode="json")
    for fragment in raw["fragments"]:
        fragment["outcome_ref"] = outcome_ref(options["run_id"]).model_dump(mode="json")
    raw["profiles"].append(deepcopy(raw["profiles"][0]))
    raw["profiles"][1]["profile_id"] = "voice-b"
    raw["profiles"][1]["synthesis"]["voice_id"] = "fixture-b"
    settings = ComparisonConfig.model_validate(raw)
    journal = AudioJournal(repo, project, settings.run_id)
    manifest = prepare(journal, settings)
    assert len(manifest["cells"]) == 4 and manifest["expected_samples"] == 20
    for fragment in settings.fragments:
        saved = journal.get("source:" + fragment.fragment_id)
        assert (
            saved["source"]["utterances"][0]["text"]
            == script["utterances"][fragment.selections[0].utterance_index]["text"]
        )
        assert saved["purpose"] == "evaluation" and not saved["production_ready"]
        assert set(saved["reading_refs"]) == {"reader", "fact"}
    changed = settings.model_copy(update={"max_attempts": 99})
    with pytest.raises(Conflict, match="configuration_changed"):
        prepare(journal, changed)
    transport = FakeAudioTransport()
    monkeypatch.setattr(audio_evaluation_cli, "HttpSynthesisTransport", lambda: transport)
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "not-written-to-artifacts")
    path = tmp_path / "audio-config.json"
    path.write_text(settings.model_dump_json())
    output = tmp_path / "audio-output"
    common = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    args = ["--config", str(path), "--export-dir", str(output)]
    assert (
        audio_evaluation_cli.main([*common, "run", *args]) == 2
    )  # Four available cells do not claim the missing sixteen.
    assert len(transport.calls) == 4
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert audio_evaluation_cli.main([*common, "resume", *args]) == 2
    assert len(transport.calls) == 4
    assert len(list((output / "blind").glob("*.wav"))) == 4
    assert len(list((output / "private/sources").glob("*.json"))) == 2
    receipt = list((output / "private").glob("*/take-1/receipt.json"))[0]
    assert json.loads(receipt.read_text())["headers"]["character-cost"] == "123"
    assert "not-written-to-artifacts" not in "".join(p.read_text() for p in output.rglob("*.json"))
