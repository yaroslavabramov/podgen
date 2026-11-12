"""Actual PostgreSQL CLI path; model replies are mechanical fixtures, never acoustic ground truth."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import audio_adjudication_cli
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewConfig, prefix
from podgen.runtime.evaluation.audio.fidelity.processing import revisions
from tests.test_audio_adjudication_runtime import Reviewer


def assert_persisted_adjudication(journal, project, asr_config, folder, monkeypatch):
    prompt = Path(__file__).resolve().parents[2] / "evals/audio/listening/local-fidelity-review.prompt.md"
    config = ReviewConfig(
        review_id=uuid4(),
        comparison_run_id=journal.run_id,
        fidelity_audit_id=asr_config.audit_id,
        candidate_result_ref=journal.ref(revisions(journal, asr_config.audit_id)[-1]["key"] + ":result"),
        prompt_path=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        price=dict(
            usd_per_million_input_tokens="1",
            usd_per_million_output_tokens="2",
            source_url="https://example.org/fixture",
            checked_at="2026-11-12",
        ),
        reservation_per_request_usd="0.10",
        max_cost_usd="1",
    )
    path = folder / "review-config.json"
    path.write_text(config.model_dump_json())
    output = folder / "review-export"
    args = [
        "--project",
        str(project),
        "--actor",
        str(journal.repo.actor_id),
        "--objects",
        str(journal.repo.objects.root),
    ]
    common = ["--config", str(path), "--export-dir", str(output)]
    transport = Reviewer()
    monkeypatch.setattr(audio_adjudication_cli, "ReviewerTransport", lambda config: transport)
    monkeypatch.setenv("JEMINI_API_KEY", "fixture-review-secret")
    assert audio_adjudication_cli.main([*args, "prepare", *common]) == 0 and not transport.calls
    assert audio_adjudication_cli.main([*args, "run", *common]) == 0
    manifest = journal.get(prefix(config.review_id) + ":manifest")
    assert len(transport.calls) == len(manifest["windows"]) and transport.calls
    monkeypatch.delenv("JEMINI_API_KEY")
    assert audio_adjudication_cli.main([*args, "resume", *common]) == 0
    monkeypatch.setattr(
        audio_adjudication_cli,
        "ReviewerTransport",
        lambda config: pytest.fail("offline path constructed transport"),
    )
    assert (
        audio_adjudication_cli.main([*args, "reprocess", *common, "--reason", "Verify frozen response again"])
        == 0
    )
    result = json.loads((output / "decisions.json").read_text())
    assert {r["candidate_id"] for r in result["decisions"]} == set(manifest["expected_case_ids"])
    assert result["whole_recording_review"] == "not_performed" and not result["production_ready"]
    assert (output / "private/prompt.md").read_bytes() == prompt.read_bytes()
    assert "fixture-review-secret" not in "".join(p.read_text() for p in output.rglob("*.json"))
    from tests.audio_adjudication_fallback_cases import assert_persisted_fallback

    assert_persisted_fallback(journal, config, args, folder, monkeypatch)
