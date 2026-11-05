"""The ordinary CLI entry reads real frozen author inputs, never staging evaluator files."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import writing_cli
from podgen.persistence.editorial_staging import EditorialStaging
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from tests.test_writing_evaluation import FakeProvider

ROOT = Path(
    os.environ.get(
        "PODGEN_TEST_EDITORIAL_ROOT",
        Path(__file__).resolve().parents[2] / "evals/cases/development/wp02-batch-01",
    )
)


@pytest.mark.parametrize("anchor_policy", ["strict", "bounded-selection-v1"])
def test_real_frozen_cli_entry_all_stages_and_evaluator_export(
    repo, scope, tmp_path, monkeypatch, capsys, anchor_policy
):
    project, _ = scope
    report = EditorialStaging(repo).import_directory(project, ROOT)
    profile = create_profile(
        repo,
        project,
        model="gpt-5.4-2026-03-05",
        context_limit=1050000,
        output_limit=32768,
        reasoning_effort="medium",
    )
    provider = FakeProvider(count=200000, fault="anchor" if anchor_policy != "strict" else None)
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    for case in ("gps-clock", "snow-pump"):
        run_id = uuid4()
        out = tmp_path / case
        args = [
            "--project",
            str(project),
            "--actor",
            str(repo.actor_id),
            "--objects",
            str(repo.objects.root),
            "run",
            "--run-id",
            str(run_id),
            "--case",
            case,
            "--author-ref",
            json.dumps(report["author_inputs"][case]),
            "--writer-profile",
            json.dumps(profile.ref.model_dump(mode="json")),
            "--reviewer-profile",
            json.dumps(profile.ref.model_dump(mode="json")),
            "--review-anchor-policy",
            anchor_policy,
            "--export-dir",
            str(out),
        ]
        # Fake provider honors the format declared in the actual material payload.
        assert writing_cli.main(args) == 0
        assert (
            (out / "draft.txt").is_file()
            and (out / "revise.txt").is_file()
            and (out / "review.json").is_file()
        )
        journal = EvaluationJournal(repo, project)
        manifest = journal.manifest(run_id)
        assert manifest["command"]["author_ref"] == report["author_inputs"][case]
        assert manifest["output_language"] == "ru"
        assert manifest["semantic_acceptance"] is False
        for stage in ("draft", "review", "revise"):
            request = json.loads((out / f"archive/{stage}/request.json").read_text())
            payload = json.loads(request["input"][0]["content"][0]["text"])
            assert payload["output_language"] == "ru"
            assert "output_language" in request["instructions"]
            docs = payload["author_materials"]["documents"]
            assert len(docs) == (8 if case == "gps-clock" else 7)
            assert all("evaluator-only" not in doc["path"] for doc in docs)
            assert len(request["input"][0]["content"][0]["text"].encode()) > 500000
            assert request["model"] == "gpt-5.4-2026-03-05"
        assert writing_cli.main(args) == 0
    assert len(provider.requests) == (6 if anchor_policy == "strict" else 8)
    assert all(
        json.loads(req["input"][0]["content"][0]["text"])["output_language"] == "ru"
        for req in provider.requests
    )
