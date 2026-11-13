import json
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.repository import Repository
from podgen.research.hosted.contracts import Approval, GeminiProfile, ResearchConfig
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.service import advance, approve, prepare
from tests.hosted_research_support import Provider, response


def test_research_journal_survives_reopen_and_enforces_project_scope(repo, scope, monkeypatch):
    project, _ = scope
    run = uuid4()
    production_repo = Repository(repo.database, repo.objects, repo.actor_id)
    journal = ResearchJournal(production_repo, project, run)
    monkeypatch.setenv("RESEARCH_FIXTURE_KEY", "fixture")
    config = ResearchConfig(
        run_id=run,
        assignment={
            "user_request": "Тема исследования",
            "output_language": "ru",
            "scope": {"source_mode": "open_web"},
        },
        instructions="Research this topic.",
        profile=GeminiProfile(agent="deep-research-preview-04-2026", credential_env="RESEARCH_FIXTURE_KEY"),
    )
    manifest = prepare(journal, config)
    approve(
        journal,
        Approval(
            request_hash=manifest["request_hash"],
            reason="Fixture approval",
            accepts_variable_provider_cost=True,
        ),
    )
    raw = response(
        "completed", steps=[dict(type="model_output", content=[dict(type="text", text="Полный отчёт.")])]
    )
    provider = Provider(response(), raw)
    assert advance(journal, provider)["state"] == "in_progress"
    reopened = ResearchJournal(production_repo, project, run)
    assert advance(reopened, provider)["state"] == "completed"
    assert [name for name, _ in provider.calls] == ["submit", "get"]
    result = reopened.get("result")
    assert reopened.bytes("result", result["report_text_blob"]).decode() == "Полный отчёт."
    assert json.loads(reopened.bytes("result", result["raw_response_blob"]))["id"] == "job/one"
    stranger = ResearchJournal(Repository(repo.database, repo.objects, uuid4()), project, run)
    with pytest.raises(AccessDenied):
        stranger.get("result")
    with pytest.raises(AccessDenied):
        production_repo.read_runtime_blob(
            project, reopened.ref("manifest"), BlobRef.model_validate(result["report_text_blob"])
        )
