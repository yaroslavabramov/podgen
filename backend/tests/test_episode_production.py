import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.episodes.production.contracts import ProductionConfig
from podgen.episodes.production.control import ProductionSuperseded
from podgen.episodes.production.export import export, status
from podgen.episodes.production.journal import ProductionJournal
from podgen.episodes.production.prepare import prepare
from podgen.episodes.production.research import terminal_question
from podgen.persistence.database import one, query
from podgen.research.hosted.transport import Response
from tests.episode_production_support import setup_episode
from tests.hosted_research_support import Provider


def test_real_topic_to_native_reviews_audio_assembly_and_idempotent_export(
    repo, scope, producer, monkeypatch, tmp_path
):
    config, runner, hosted, models, speech, fetcher = setup_episode(repo, scope, producer, monkeypatch)
    from podgen.writing.source_tools import SourceCorpus

    original = SourceCorpus.__init__
    prepared_searches = []

    def initialize(corpus, materials, **kwargs):
        index = kwargs.get("index")
        if index is not None:
            # The constructor starts retrieval preparation, so binding after
            # this call is too late for the first document's index writes.
            assert index.access.run_id in index.journal.execution.sets
            prepared_searches.append(index.access.run_id)
        return original(corpus, materials, **kwargs)

    monkeypatch.setattr(SourceCorpus, "__init__", initialize)
    prepared = prepare(repo, scope[0], config)
    assert prepared["revision"] == 0 and not hosted.calls and not models.requests and not speech.calls
    result = runner.run()
    assert prepared_searches
    assert result["status"] == result["phase"] == "assembled"
    assert result["progress"]["revisions_used"] == 1
    assert [name for name, _ in hosted.calls] == ["submit"]
    assert len(fetcher.calls) == len(speech.calls) == 1
    stages = [body["metadata"]["podgen_stage"] for body in models.requests]
    assert {
        "plan",
        "plan-review",
        "plan-revise",
        "plan-review-revised",
        "draft",
        "reader-review",
        "fact-review",
        "revise-1",
        "reader-review-1",
        "fact-review-1",
    } == set(stages)
    assert status(result)["production_ready"] is False
    before = (len(hosted.calls), len(models.requests), len(speech.calls), result["epoch"], result["revision"])
    assert runner.run() == result
    assert runner.journal.request("running") == result
    assert runner.journal.request("paused") == result
    exported = export(runner.journal, tmp_path / "episode")
    assert exported["state"] == "assembled" and exported["acoustic_review"] == "pending"
    assert (tmp_path / "episode/audio/master.wav").stat().st_size > 1000
    assert json.loads((tmp_path / "episode/status.json").read_text())["production_ready"] is False
    assert before == (
        len(hosted.calls),
        len(models.requests),
        len(speech.calls),
        runner.journal.read()["epoch"],
        runner.journal.read()["revision"],
    )


def test_config_prepare_pause_cas_and_fresh_authorization(repo, scope, producer, monkeypatch):
    config, runner, *_ = setup_episode(repo, scope, producer, monkeypatch)
    prepare(repo, scope[0], config)
    assert prepare(repo, scope[0], config) == runner.journal.read()
    with pytest.raises(Conflict, match="configuration_changed"):
        runner.journal.prepare(config.model_copy(update=dict(topic="Changed")))
    with pytest.raises(ContractError, match="run_mismatch"):
        ProductionJournal(repo, scope[0], uuid4()).prepare(config)
    paused = runner.journal.request("paused")
    assert runner.journal.request("paused") == paused
    stopped = runner.run()
    assert stopped["status"] == "paused" and stopped["epoch"] == 0
    assert runner.run() == stopped
    runner.journal.request("running")
    claimed = runner.journal.claim()
    with pytest.raises(ProductionSuperseded):
        runner.journal.transition(claimed["epoch"] - 1, {}, "writing", "running")
    with runner.journal.lock(), pytest.raises(Conflict, match="busy"):
        with runner.journal.lock():
            pytest.fail("second runner acquired the lock")
    with repo.database.transaction() as tx:
        query(
            tx, "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a", p=scope[0], a=repo.actor_id
        )
    with pytest.raises(AccessDenied):
        runner.journal.check_dispatch(claimed["epoch"])


def test_invalid_settings_rejected_before_prepare(repo, scope, producer, monkeypatch):
    config, *_ = setup_episode(repo, scope, producer, monkeypatch)
    for mutation in (
        dict(audio_max_cost_usd="0"),
        dict(audio_max_cost_usd="no"),
        dict(audio_max_cost_usd="NaN"),
        dict(max_research_jobs=None),
        dict(writer_profile_ref=dict(kind="Policy", artifact_id=str(uuid4()), revision_id=str(uuid4()))),
    ):
        raw = config.model_dump(mode="json")
        raw["settings"].update(mutation)
        with pytest.raises(ValidationError):
            ProductionConfig.model_validate(raw)
    raw = config.model_dump(mode="json")
    raw["output_language"] = "en"
    with pytest.raises(ValidationError, match="voice_language_mismatch"):
        ProductionConfig.model_validate(raw)
    with repo.database.read_transaction() as tx:
        assert (
            one(tx, "SELECT count(*) n FROM episode_production_runs WHERE run_id=:r", r=config.run_id)["n"]
            == 0
        )


def test_known_review_questions_are_one_lossless_research_assignment():
    questions = [dict(question=f"Question {i}?", why_needed="Причина " * 10000) for i in range(3)]
    combined = terminal_question(questions)
    assert json.loads(combined["question"].split("\n", 1)[1]) == questions
    reordered = [dict(reversed(list(question.items()))) for question in questions]
    assert terminal_question(reordered) == combined


@pytest.mark.parametrize(
    "response,code",
    [(Response(429, {}, b"quota"), "provider_rejected"), (Response(200, {}, b"{}"), "outcome_unknown")],
)
def test_stopped_research_is_not_polled_or_resubmitted_and_exact_reason_is_retained(
    repo, scope, producer, monkeypatch, response, code
):
    config, runner, *_ = setup_episode(repo, scope, producer, monkeypatch)
    provider = Provider(response)
    runner.research_provider = provider
    prepare(repo, scope[0], config)
    for _ in range(2):
        with pytest.raises(ContractError, match="episode_research_" + code):
            runner.run()
        row = runner.journal.read()
        assert (
            row["status"] == "needs_attention" and row["progress"]["error_code"] == "episode_research_" + code
        )
    assert [name for name, _ in provider.calls] == ["submit"]
    with repo.database.read_transaction() as tx:
        event = one(
            tx,
            "SELECT payload FROM episode_production_events WHERE project_id=:p AND run_id=:r ORDER BY revision DESC LIMIT 1",
            p=scope[0],
            r=config.run_id,
        )
    assert event["payload"]["error_code"] == "episode_research_" + code


def test_pause_during_paid_research_captures_reply_before_stopping_next_action(
    repo, scope, producer, monkeypatch
):
    config, runner, hosted, models, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    submit = hosted.submit

    def pause_after_reply(*args):
        reply = submit(*args)
        runner.journal.request("paused")
        return reply

    hosted.submit = pause_after_reply
    prepare(repo, scope[0], config)
    stopped = runner.run()
    assert stopped["phase"] == "materials" and stopped["status"] == "paused"
    assert len(hosted.calls) == 1 and not models.requests and not speech.calls
    runner.journal.request("running")
    assert runner.run()["status"] == "assembled" and len(hosted.calls) == 1


def test_successful_resume_clears_current_error_and_preserves_failure_history(
    repo, scope, producer, monkeypatch
):
    config, runner, hosted, *_ = setup_episode(repo, scope, producer, monkeypatch)
    prepare(repo, scope[0], config)
    material = runner._materials

    def fail_once():
        raise ContractError("exact_fixture_reason", "Detailed error must not enter the compact head")

    monkeypatch.setattr(runner, "_materials", fail_once)
    with pytest.raises(ContractError, match="exact_fixture_reason"):
        runner.run()
    assert status(runner.journal.read())["error_code"] == "exact_fixture_reason"
    monkeypatch.setattr(runner, "_materials", material)
    result = runner.run()
    assert result["status"] == "assembled" and "error_code" not in result["progress"]
    assert len(hosted.calls) == 1
    with repo.database.read_transaction() as tx:
        assert (
            one(
                tx,
                "SELECT count(*) n FROM episode_production_events WHERE project_id=:p AND run_id=:r AND payload->>'error_code'='exact_fixture_reason'",
                p=scope[0],
                r=config.run_id,
            )["n"]
            > 0
        )
