"""Actual PostgreSQL, immutable blob/ACL storage and native approved text; only providers are doubled."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from podgen import audio_evaluation_cli, writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.audio.contracts import ComparisonConfig
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.evaluation.audio.prepare import prepare
from podgen.runtime.evaluation.reading_cycle import outcome_ref
from tests.audio_cases import config, profile, response, signal
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
    monkeypatch.setattr(audio_evaluation_cli, "HttpSynthesisTransport", lambda **kwargs: transport)
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

    # A separate offline edition reads actual persisted blobs through their owning record ACL.
    batch = str(uuid4())
    edition = tmp_path / "listening-edition"
    listen_args = ["--config", str(path), "--export-dir", str(edition), "--batch-id", batch]
    monkeypatch.setattr(
        audio_evaluation_cli,
        "HttpSynthesisTransport",
        lambda: pytest.fail("offline edition constructed a transport"),
    )
    assert audio_evaluation_cli.main([*common, "listen", *listen_args]) == 0
    assert audio_evaluation_cli.main([*common, "listen-export", *listen_args]) == 0
    status = json.loads((edition / "status.json").read_text())
    assert status["coverage"]["complete_http_200"] == 4
    assert status["listening_samples"] == 4 and not status["requested_matrix_complete"]
    assert len(transport.calls) == 4

    # /2 imports one complete approved script, without inventing a second fragment.
    targeted = settings.model_dump(mode="json")
    targeted.update(
        contract="audio-comparison/2",
        run_id=str(uuid4()),
        providers=["gemini"],
        expected_voices_per_provider={"gemini": 1},
        request_timeout_seconds=600,
    )
    targeted["profiles"] = [deepcopy(targeted["profiles"][0])]
    targeted["profiles"][0]["synthesis"] = profile("gemini").model_dump(mode="json")
    targeted["fragments"] = [deepcopy(targeted["fragments"][0])]
    targeted["fragments"][0]["selections"] = [
        dict(utterance_index=i) for i in range(len(script["utterances"]))
    ]
    target_path = tmp_path / "targeted.json"
    target_path.write_text(json.dumps(targeted))
    target_output = tmp_path / "targeted-output"
    target_args = ["--config", str(target_path), "--export-dir", str(target_output)]
    timeouts = []

    def target_transport(**kwargs):
        timeouts.append(kwargs["timeout_seconds"])
        return transport

    monkeypatch.setattr(audio_evaluation_cli, "HttpSynthesisTransport", target_transport)
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "not-written-to-artifacts")
    assert audio_evaluation_cli.main([*common, "run", *target_args]) == 0
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert audio_evaluation_cli.main([*common, "resume", *target_args]) == 0
    assert timeouts == [600, 600] and len(transport.calls) == 5
    target_status = json.loads((target_output / "status.json").read_text())
    target_manifest = json.loads((target_output / "private/manifest.json").read_text())
    assert (
        target_status["expected_samples"]
        == target_status["prepared_samples"]
        == target_status["listening_samples"]
        == 1
    )
    assert (
        target_status["requested_matrix_complete"]
        and not target_status["full_five_provider_comparison_complete"]
    )
    assert target_manifest["availability"] == [
        dict(provider="gemini", configured_profiles=1, expected_profiles=1, status="profiles_configured")
    ]
    target_source = json.loads(next((target_output / "private/sources").glob("*.json")).read_text())
    assert target_source["source"]["utterances"] == [
        dict(u, utterance_id=target_source["source"]["utterances"][i]["utterance_id"])
        for i, u in enumerate(script["utterances"])
    ]
    # The listening export derives its coverage from the same 1x1 manifest.
    target_listen = [
        "--config",
        str(target_path),
        "--export-dir",
        str(tmp_path / "targeted-listening"),
        "--batch-id",
        str(uuid4()),
    ]
    assert audio_evaluation_cli.main([*common, "listen", *target_listen]) == 0
    target_listening_status = json.loads((tmp_path / "targeted-listening/status.json").read_text())
    assert target_listening_status["expected_samples"] == target_listening_status["listening_samples"] == 1
    assert target_listening_status["requested_matrix_complete"] and len(transport.calls) == 5
