import json
import struct
from copy import deepcopy
from uuid import uuid4

import pytest

from podgen.audio.media.wav_repair import repair_fish_stream
from podgen.audio.synthesis.registry import adapter
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.audio import results
from podgen.runtime.evaluation.audio.attempts import execute, prefix
from podgen.runtime.evaluation.audio.listening.batch import ListeningConfig, batch_key, build
from podgen.runtime.evaluation.audio.listening.export import export_batch
from tests.audio_cases import profile, response, signal, source
from tests.test_audio_evaluation import fixture


def placeholder():
    data = bytearray(signal())
    struct.pack_into("<I", data, 4, 0xFFFFFF24)
    struct.pack_into("<I", data, 40, 0xFFFFFF00)
    return bytes(data)


def test_observed_fish_repair_changes_only_lengths_and_preserves_pcm():
    raw = placeholder()
    repaired, audit = repair_fish_stream(raw, complete=True, status_code=200, sample_rate_hz=24000)
    assert repaired[:4] == raw[:4] and repaired[8:40] == raw[8:40] and repaired[44:] == raw[44:]
    assert [p["offset"] for p in audit["patches"]] == [4, 40]
    assert audit["payload_unchanged"] and audit["speech_completeness"] == "pending_listening"
    for data, complete, status, rate in (
        (raw, False, 200, 24000),
        (raw, True, 206, 24000),
        (raw[:-1], True, 200, 24000),
        (raw, True, 200, 44100),
        (signal()[:-500], True, 200, 24000),
        (raw[:40] + b"\xff" * 4 + raw[44:], True, 200, 24000),
    ):
        with pytest.raises(ContractError):
            repair_fish_stream(data, complete=complete, status_code=status, sample_rate_hz=rate)


def test_batch_recovers_failed_normalization_and_freezes_shared_target_offline(monkeypatch, tmp_path):
    j, c, m, first = fixture(monkeypatch)
    second = dict(first, cell_id=str(uuid4()))
    absent = dict(first, cell_id=str(uuid4()))
    m["cells"] += [second, absent]

    # A sparse full-scale transient forces a lower common gain target.
    class Transport:
        calls = 0

        def submit(self, request, credential):
            self.calls += 1
            return response(request.provider, signal(extremes=self.calls == 2))

    transport = Transport()
    monkeypatch.setattr(
        results, "normalize", lambda *a: (_ for _ in ()).throw(ContractError("old_normalizer_refusal"))
    )
    for cell in (first, second):
        assert execute(j, m, cell, c, transport) == "processing_failed"
    before = deepcopy(j.records)
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    monkeypatch.setattr("podgen.runtime.evaluation.audio.listening.batch.prepare", lambda *a: m)
    cfg = ListeningConfig(batch_id=uuid4())
    binding = build(j, c, cfg)
    saved = j.get(batch_key(cfg.batch_id) + ":manifest")
    assert len(saved["samples"]) == 2 and len(saved["excluded"]) == 1
    assert saved["target_lufs"] < -20
    for sample in saved["samples"]:
        value = j.get(batch_key(cfg.batch_id) + ":result:" + sample["value"]["cell"]["cell_id"])
        assert value["normalization"]["target_lufs"] == saved["target_lufs"]
        assert value["normalization"]["true_peak_dbtp"] <= -1
        assert sample["value"]["previous_processing_errors"]
    assert all(j.records[k] == v for k, v in before.items()) and transport.calls == 2
    assert build(j, c, cfg) == binding
    with monkeypatch.context() as changed_tools:
        changed_tools.setattr(
            "podgen.runtime.evaluation.audio.listening.batch.tools", lambda: {"ffmpeg": "other-version"}
        )
        with pytest.raises(Conflict, match="tool_versions_changed"):
            build(j, c, cfg)
    with pytest.raises(Conflict):
        build(j, c, cfg.model_copy(update=dict(headroom_db=0.5)))
    report = export_batch(j, cfg.batch_id, tmp_path / "edition")
    assert report["listening_samples"] == 2 and report["expected_samples"] == 20
    assert not report["requested_matrix_complete"]
    assert report["measured_playback"]["all_same_target"]
    assert report["measured_playback"]["max_true_peak_dbtp"] <= -1
    assert "elevenlabs" not in (tmp_path / "edition/blind/index.html").read_text()
    assert json.loads((tmp_path / "edition/batch.json").read_text()) == binding
    other = cfg.model_copy(update=dict(batch_id=uuid4()))
    build(j, c, other)
    with pytest.raises(Conflict):
        export_batch(j, other.batch_id, tmp_path / "edition")


def test_fish_header_repair_is_explicit_and_audited_in_separate_batch(monkeypatch, tmp_path):
    j, c, m, cell = fixture(monkeypatch)
    candidate = c.profiles[0].model_copy(update=dict(synthesis=profile("fish")))
    c = c.model_copy(update=dict(profiles=[candidate]))
    cell["request"] = adapter("fish").prepare(candidate.synthesis, source()).model_dump(mode="json")

    class Transport:
        def submit(self, *args):
            return response("fish", placeholder())

    assert execute(j, m, cell, c, Transport()) == "processing_failed"
    key = prefix(cell["cell_id"], 1)
    original = deepcopy(j.get(key + ":audio"))
    monkeypatch.setattr("podgen.runtime.evaluation.audio.listening.batch.prepare", lambda *a: m)
    with pytest.raises(ContractError, match="no_decoded_audio"):
        build(j, c, ListeningConfig(batch_id=uuid4()))
    cfg = ListeningConfig(batch_id=uuid4(), repair_observed_fish_header=True)
    build(j, c, cfg)
    report = export_batch(j, cfg.batch_id, tmp_path / "edition")
    assert report["listening_samples"] == 1
    assert report["measured_playback"]["container_repairs"] == 1
    assert report["measured_playback"]["all_repaired_pcm_payloads_unchanged"]
    audit = json.loads((tmp_path / "edition/private/S001/decoded.json").read_text())
    assert audit["repair"]["payload_unchanged"] and audit["receipt_ref"] == j.ref(
        key + ":receipt"
    ).model_dump(mode="json")
    assert j.get(key + ":audio") == original
    assert j.bytes(key + ":audio", original["provider_audio_blob"]) == placeholder()
