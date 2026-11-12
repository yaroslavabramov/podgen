import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen import audio_local_replacement_cli as cli
from podgen.audio.http import HttpReceipt
from podgen.runtime.evaluation.audio.local_replacement.service import synthesize
from tests.audio_local_replacement_fixtures import Synthesis, allow, fixture


@pytest.mark.parametrize("blocked", ["credential_missing", "budget_exhausted"])
def test_cli_preserves_operation_result_separately_without_new_network(
    tmp_path, monkeypatch, capsys, blocked
):
    j, c, _ = fixture(monkeypatch)
    allow(j, c, max_requests=1)
    speech = Synthesis(HttpReceipt(429, {}, b"refused", 1))
    operation = "synthesize"
    extra = []
    before = 0
    if blocked == "credential_missing":
        monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    else:
        assert synthesize(j, c.proposal_id, speech) == "provider_rejected"
        before = 1
        operation = "retry"
        extra = ["--reason", "Explicit retry beyond approved budget"]
    monkeypatch.setenv("PODGEN_DATABASE_URL", "fixture-unused")
    monkeypatch.setattr(
        cli, "Database", lambda *args: SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))
    )
    monkeypatch.setattr(cli, "Repository", lambda *args, **kwargs: j.repo)
    monkeypatch.setattr(cli, "AudioJournal", lambda *args, **kwargs: j)
    monkeypatch.setattr(cli, "HttpSynthesisTransport", lambda **kwargs: speech)
    folder = tmp_path / "proposal"
    code = cli.main(
        [
            "--project",
            str(uuid4()),
            "--actor",
            str(j.repo.actor_id),
            "--run-id",
            str(j.run_id),
            "--objects",
            str(tmp_path),
            operation,
            "--proposal-id",
            str(c.proposal_id),
            "--export-dir",
            str(folder),
            *extra,
        ]
    )
    assert code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["operation_result"] == blocked
    assert result["delivery_state"] == ("not_submitted" if before == 0 else "provider_rejected")
    assert json.loads((folder / "status.json").read_text())["operation_result"] == blocked
    assert len(speech.calls) == before


@pytest.mark.parametrize(
    "asr_status", ["asr_credential_missing", "asr_outcome_unknown", "localization_failed"]
)
def test_cli_resume_exposes_asr_state_without_repaying_tts(tmp_path, monkeypatch, capsys, asr_status):
    from podgen.runtime.evaluation.audio.local_replacement.prepare import prepare
    from tests.audio_local_replacement_fixtures import Recognition

    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    proposal = prepare(j, c)
    tts = Synthesis()
    receipt = (
        HttpReceipt(503, {}, b"unknown", 1)
        if asr_status == "asr_outcome_unknown"
        else HttpReceipt(200, {}, b"{}", 1)
    )
    asr = Recognition(proposal["payload"]["synthesis_input"]["utterances"], receipt=receipt)
    monkeypatch.setenv("PODGEN_DATABASE_URL", "fixture-unused")
    monkeypatch.setenv("FISH_API_KEY", "fixture")
    if asr_status == "asr_credential_missing":
        monkeypatch.delenv("FISH_API_KEY")
    monkeypatch.setattr(
        cli, "Database", lambda *args: SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))
    )
    monkeypatch.setattr(cli, "Repository", lambda *args, **kwargs: j.repo)
    monkeypatch.setattr(cli, "AudioJournal", lambda *args, **kwargs: j)
    monkeypatch.setattr(cli, "HttpSynthesisTransport", lambda **kwargs: tts)
    monkeypatch.setattr(cli, "RawAudioTransport", lambda **kwargs: asr)
    common = [
        "--project",
        str(uuid4()),
        "--actor",
        str(j.repo.actor_id),
        "--run-id",
        str(j.run_id),
        "--objects",
        str(tmp_path),
    ]
    for operation in ("synthesize", "resume"):
        assert (
            cli.main(
                [
                    *common,
                    operation,
                    "--proposal-id",
                    str(c.proposal_id),
                    "--export-dir",
                    str(tmp_path / "proposal"),
                ]
            )
            == 2
        )
        result = json.loads(capsys.readouterr().out)
        assert result["operation_result"] == asr_status
        assert result["delivery_state"] == (
            "asr_pending" if asr_status == "asr_credential_missing" else asr_status
        )
    assert len(tts.calls) == 1
    assert len(asr.calls) == (0 if asr_status == "asr_credential_missing" else 1)
