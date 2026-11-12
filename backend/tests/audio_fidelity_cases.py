"""Exercise recognition against the approved subject created by the native audio runtime test."""

import json
from uuid import UUID, uuid4

import pytest

from podgen import audio_fidelity_cli
from podgen.audio.http import HttpReceipt
from podgen.runtime.evaluation.audio.attempts import prefix as sample_prefix
from podgen.runtime.evaluation.audio.fidelity.contracts import FidelityConfig, prefix
from podgen.runtime.evaluation.audio.journal import AudioJournal


def assert_persisted_fidelity(repo, project, comparison, folder, monkeypatch):
    journal = AudioJournal(repo, project, UUID(comparison["run_id"]))
    cell = journal.get("manifest")["cells"][0]
    config = FidelityConfig(
        audit_id=uuid4(),
        comparison_run_id=journal.run_id,
        result_ref=journal.ref(sample_prefix(cell["cell_id"], 1) + ":result"),
        reservation_usd="0.02",
        price=dict(
            usd_per_audio_minute="0.01", source_url="https://example.org/fixture", checked_at="2026-11-12"
        ),
    )
    calls = []

    class ASR:
        def submit(self, request, body, credential):
            calls.append(body)
            if len(calls) == 1:
                return HttpReceipt(503, {}, b"service unavailable", 5)
            return HttpReceipt(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    dict(
                        text="Не 35 недель",
                        duration=5,
                        segments=[
                            dict(text="Не", start=0.1, end=0.2),
                            dict(text="35", start=0.3, end=0.5),
                            dict(text="недель", start=0.6, end=0.9),
                        ],
                        request_id="native-fixture",
                    )
                ).encode(),
                10,
            )

    monkeypatch.setattr(audio_fidelity_cli, "RawAudioTransport", lambda **kwargs: ASR())
    monkeypatch.setenv("FISH_API_KEY", "fixture-recognition-secret")
    path = folder / "fidelity.json"
    path.write_text(config.model_dump_json())
    common = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    args = ["--config", str(path), "--export-dir", str(folder / "fidelity-export")]
    assert audio_fidelity_cli.main([*common, "prepare", *args]) == 0 and not calls
    assert audio_fidelity_cli.main([*common, "run", *args]) == 2
    assert audio_fidelity_cli.main([*common, "resume", *args]) == 2 and len(calls) == 1
    assert (
        audio_fidelity_cli.main(
            [
                *common,
                "retry",
                *args,
                "--reason",
                "Explicit service recovery",
                "--max-total-requests",
                "2",
                "--max-total-cost-usd",
                "0.04",
            ]
        )
        == 0
    )
    monkeypatch.delenv("FISH_API_KEY")
    assert audio_fidelity_cli.main([*common, "resume", *args]) == 0 and len(calls) == 2
    monkeypatch.setattr(
        audio_fidelity_cli,
        "RawAudioTransport",
        lambda **kwargs: pytest.fail("offline path constructed transport"),
    )
    assert audio_fidelity_cli.main([*common, "reprocess", *args, "--reason", "Replay saved receipt"]) == 0
    manifest = journal.get(prefix(config.audit_id) + ":manifest")
    assert calls[0] == journal.bytes(prefix(config.audit_id) + ":manifest", manifest["wire_blob"])
    assert manifest["transport_proof"]["pcm_bit_identical"]
    assert manifest["source_script_hash"] == manifest["source"]["source_script_hash"]
    assert json.loads((folder / "fidelity-export/status.json").read_text())["requests_submitted"] == 2
    assert "fixture-recognition-secret" not in "".join(
        p.read_text() for p in (folder / "fidelity-export").rglob("*.json")
    )

    from podgen.domain.errors import ContractError
    from podgen.runtime.evaluation.audio.fidelity import processing as fidelity_processing
    from podgen.runtime.evaluation.audio.fidelity.export import export as export_fidelity
    from podgen.runtime.evaluation.audio.fidelity.service import reprocess as reprocess_fidelity

    current = export_fidelity(journal, config, folder / "fidelity-export")
    last = current["current"]["current_result"]
    with monkeypatch.context() as local:
        local.setattr(
            fidelity_processing,
            "generate",
            lambda *a, **k: (_ for _ in ()).throw(ContractError("native_injected_failure")),
        )
        assert reprocess_fidelity(journal, config, "Native failed local reprocess") == "response_invalid"
    failed = export_fidelity(journal, config, folder / "fidelity-export")
    assert failed["current"]["current_result"] is None and failed["current"]["last_successful_result"] == last
    assert json.loads((folder / "fidelity-export/candidates.json").read_text())["report"] is None
