"""Native two-part assembly and ASR of its final PCM; all provider calls are fixture transports."""

import json
from uuid import UUID, uuid4

import pytest

from podgen import audio_assembly_cli, audio_evaluation_cli, audio_fidelity_cli
from podgen.audio.assembly.pcm import read
from podgen.audio.http import HttpReceipt
from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.assembly.contracts import AssemblyConfig, prefix
from podgen.runtime.evaluation.audio.assembly.subject import resolve
from podgen.runtime.evaluation.audio.attempts import prefix as sample_prefix
from podgen.runtime.evaluation.audio.fidelity.contracts import (
    FidelityConfig,
    FidelityConfigV2,
)
from podgen.runtime.evaluation.audio.fidelity.contracts import (
    prefix as audit_prefix,
)
from podgen.runtime.evaluation.audio.fidelity.prepare import prepare as prepare_asr
from podgen.runtime.evaluation.audio.journal import AudioJournal
from tests.audio_cases import response, signal


def assert_native_assembly(repo, project, comparison, script, folder, monkeypatch):
    raw = dict(comparison, run_id=str(uuid4()), max_attempts=2)
    midpoint = len(script["utterances"]) // 2
    raw["fragments"] = [
        dict(
            fragment_id=f"part-{i + 1}",
            outcome_ref=comparison["fragments"][0]["outcome_ref"],
            selections=[dict(utterance_index=n) for n in indexes],
        )
        for i, indexes in enumerate((range(midpoint), range(midpoint, len(script["utterances"]))))
    ]
    path = folder / "assembly-parts.json"
    path.write_text(json.dumps(raw))

    class Speech:
        calls = 0

        def submit(self, request, credential):
            self.calls += 1
            return response(request.provider, signal())

    speech = Speech()
    monkeypatch.setattr(audio_evaluation_cli, "HttpSynthesisTransport", lambda **kwargs: speech)
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "fixture-key")
    common = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    assert (
        audio_evaluation_cli.main(
            [*common, "run", "--config", str(path), "--export-dir", str(folder / "assembly-parts")]
        )
        == 0
    )
    assert speech.calls == 2
    journal = AudioJournal(repo, project, UUID(raw["run_id"]))
    manifest = journal.get("manifest")
    part_refs = [journal.ref(sample_prefix(c["cell_id"], 1) + ":result") for c in manifest["cells"]]
    config = AssemblyConfig(
        assembly_id=uuid4(),
        comparison_run_id=journal.run_id,
        part_result_refs=part_refs,
        preferred_lufs=-22.2,
        join_context_seconds=2,
    )
    path = folder / "assembly-config.json"
    path.write_text(config.model_dump_json())
    args = ["--config", str(path), "--export-dir", str(folder / "assembly")]
    assert audio_assembly_cli.main([*common, "build", *args]) == 0
    assert audio_assembly_cli.main([*common, "build", *args]) == 0
    result = journal.get(prefix(config.assembly_id) + ":result")
    assert result["metrics"]["sample_frames"] == 480000 and result["metrics"]["sample_rate_hz"] == 48000
    assert (
        result["assembly_metrics"]["sample_frames"] == 240000
        and result["assembly_metrics"]["sample_rate_hz"] == 24000
    )
    assert result["frame_map"][0]["source_end_frame"] == 120000
    assert (
        result["frame_map"][0]["master_end_frame"] == result["frame_map"][1]["master_start_frame"] == 240000
    )
    assert result["frame_map"][1]["master_end_frame"] == 480000
    assert (
        result["joins"][0]["at_master_frame"] == 240000
        and result["joins"][0]["review_status"] == "pending_actual_audio_review"
    )
    assert result["coverage"]["utterances"] == len(script["utterances"]) and result["coverage"]["full_script"]
    assert not result["production_ready"] and result["acoustic_verdict"] == "pending"
    joined = (folder / "assembly/private/assembly-pcm-before-gain.wav").read_bytes()
    chunks = [
        journal.base.read_blob(ref, BlobRef.model_validate(journal.base.read(ref)["decoded_pcm_blob"]))
        for ref in part_refs
    ]
    assert read(joined)[2] == b"".join(read(p)[2] for p in chunks)
    ref = journal.ref(prefix(config.assembly_id) + ":result")
    assert resolve(journal, ref)["audio_blob"] == result["master_pcm_blob"]
    forged = journal.base.record(result)
    with pytest.raises(ContractError):
        resolve(journal, forged)
    fidelity = FidelityConfigV2(
        audit_id=uuid4(),
        comparison_run_id=journal.run_id,
        subject=dict(kind="assembly", ref=ref.model_dump(mode="json")),
        reservation_usd="0.02",
        price=dict(
            usd_per_audio_minute="0.01", source_url="https://example.org/fixture", checked_at="2026-11-12"
        ),
    )
    path = folder / "assembled-fidelity.json"
    path.write_text(fidelity.model_dump_json())

    class ASR:
        calls = 0

        def submit(self, *args):
            self.calls += 1
            return HttpReceipt(
                200,
                {},
                json.dumps(
                    dict(
                        text="Проверка",
                        duration=10,
                        segments=[dict(text="Проверка", start=1, end=2)],
                        request_id="assembly-fixture",
                    )
                ).encode(),
                5,
            )

    asr = ASR()
    monkeypatch.setattr(audio_fidelity_cli, "RawAudioTransport", lambda **kwargs: asr)
    monkeypatch.setenv("FISH_API_KEY", "fixture-asr")
    assert (
        audio_fidelity_cli.main(
            [*common, "run", "--config", str(path), "--export-dir", str(folder / "assembly-asr")]
        )
        == 0
    )
    assert asr.calls == 1 and speech.calls == 2
    checked = journal.get(audit_prefix(fidelity.audit_id) + ":manifest")
    assert checked["audio_variant"] == "assembled_final_playback_pcm" and checked["decoded_ref"] is None
    assert (
        checked["audio_blob"]["sha256"]
        == result["master_pcm_blob"]["sha256"]
        == result["playback_audio_blob"]["sha256"]
    )
    assert (
        checked["transport_proof"]["sample_frames"] == 480000
        and checked["transport_proof"]["pcm_bit_identical"]
    )
    old = dict(
        fidelity.model_dump(mode="json"),
        contract="audio-fidelity-evaluation/1",
        result_ref=ref.model_dump(mode="json"),
        audit_id=str(uuid4()),
    )
    old.pop("subject")
    with pytest.raises(ContractError, match="outside_comparison"):
        prepare_asr(journal, FidelityConfig.model_validate(old))

    from tests.audio_repair_cases import assert_native_repair

    assert_native_repair(journal, project, ref, part_refs, fidelity, folder, monkeypatch)
