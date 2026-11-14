"""Real PostgreSQL atomicity and complete-operation work bounds (no provider API)."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.errors import AccessDenied, Conflict
from podgen.persistence.database import one, query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.repository import Repository
from podgen.research.hosted.contracts import Approval, GeminiProfile, ResearchConfig
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.projection import intent_head
from podgen.research.hosted.service import advance, approve, prepare
from podgen.research.hosted.state import status
from tests.hosted_research_support import Provider, response


@pytest.fixture
def research(repo, scope, monkeypatch):
    project, _ = scope
    production = Repository(repo.database, repo.objects, repo.actor_id)
    journal = ResearchJournal(production, project, uuid4())
    monkeypatch.setenv("HEAD_FIXTURE_KEY", "fixture")
    config = ResearchConfig(
        run_id=journal.run_id,
        assignment=dict(
            user_request="Clock history", output_language="en", scope=dict(source_mode="open_web")
        ),
        instructions="Research the assignment.",
        profile=GeminiProfile(agent="deep-research-preview-04-2026", credential_env="HEAD_FIXTURE_KEY"),
    )
    manifest = prepare(journal, config)
    approve(
        journal,
        Approval(
            request_hash=manifest["request_hash"],
            reason="Fake-provider fixture",
            accepts_variable_provider_cost=True,
        ),
    )
    return journal


def reopen(journal):
    return ResearchJournal(journal.repo, journal.project, journal.run_id)


def test_complete_next_poll_work_is_constant_after_1_20_100_polls(research, monkeypatch, record_property):
    journal = research
    provider = Provider(*(response(steps=[dict(content="historical body " * 100)]) for _ in range(105)))
    work = Counter()
    measurements = []

    def sql(conn, cursor, statement, parameters, context, many):
        work["sql_calls"] += 1
        work["sql_rows"] += max(0, cursor.rowcount)

    original_read = journal.repo.objects.read
    original_publish = journal.repo.objects.publish

    def read(project, ref):
        value = original_read(project, ref)
        work["object_reads"] += 1
        work["object_bytes_read"] += len(value)
        return value

    def publish(project, staged):
        value = original_publish(project, staged)
        work["object_writes"] += 1
        work["object_bytes_written"] += value.byte_length
        return value

    monkeypatch.setattr(journal.repo.objects, "read", read)
    monkeypatch.setattr(journal.repo.objects, "publish", publish)
    advance(journal, provider)
    event.listen(journal.repo.database.engine, "after_cursor_execute", sql)
    try:
        for number in range(1, 101):
            if number in (1, 20, 100):
                work.clear()
                assert advance(reopen(journal), provider)["state"] == "in_progress"
                poll = dict(work)
                work.clear()
                assert status(reopen(journal))["state"] == "in_progress"
                measurements.append(dict(polls=number, poll=poll, status=dict(work)))
            else:
                advance(journal, provider)
    finally:
        event.remove(journal.repo.database.engine, "after_cursor_execute", sql)
    record_property("hosted_research_work", measurements)
    assert measurements[0]["poll"] == measurements[1]["poll"] == measurements[2]["poll"]
    assert measurements[0]["status"] == measurements[1]["status"] == measurements[2]["status"]
    assert measurements[0]["poll"]["object_reads"] == 1
    assert measurements[0]["status"].get("object_reads", 0) == 0
    assert sum(kind == "submit" for kind, _ in provider.calls) == 1


@pytest.mark.parametrize("stage", ["intent", "receipt"])
def test_failed_head_update_rolls_back_corresponding_record(research, monkeypatch, stage):
    from podgen.research.hosted import journal as module

    journal = research
    original = module.one

    def fail(tx, statement, **params):
        if "UPDATE hosted_research_heads" in statement and params["next"] == (1 if stage == "intent" else 2):
            raise RuntimeError("interruption inside exchange transaction")
        return original(tx, statement, **params)

    monkeypatch.setattr(module, "one", fail)
    provider = Provider(response(), response())
    with pytest.raises(RuntimeError):
        advance(journal, provider)
    monkeypatch.setattr(module, "one", original)
    reopened = reopen(journal)
    assert reopened.get(f"exchange:1:{stage}") is None
    assert reopened.get("job") is None
    assert reopened.head()["revision"] == (0 if stage == "intent" else 1)
    assert advance(reopened, provider)["state"] == ("in_progress" if stage == "intent" else "outcome_unknown")
    assert sum(kind == "submit" for kind, _ in provider.calls) == 1


def test_receipt_commit_survives_crash_before_result_and_reopen(research, monkeypatch):
    journal = research
    original = journal.exchange

    def fail(key, *args):
        result = original(key, *args)
        if key.endswith(":receipt"):
            raise RuntimeError("crash after committed receipt")
        return result

    monkeypatch.setattr(journal, "exchange", fail)
    provider = Provider(
        response(
            "completed", steps=[dict(type="model_output", content=[dict(type="text", text="Full report")])]
        )
    )
    with pytest.raises(RuntimeError):
        advance(journal, provider)
    reopened = reopen(journal)
    assert reopened.head()["last_exchange"]["receipt"] is True
    assert reopened.head()["job_recorded"] is True and reopened.get("job")["provider_job_id"] == "job/one"
    assert advance(reopened, provider)["state"] == "completed"
    assert len(provider.calls) == 1


def test_stale_head_writer_cannot_append_an_exchange(research):
    journal = research
    before = journal.head()
    first = intent_head(before, "submit")
    journal.exchange("exchange:1:intent", dict(operation="submit"), before, first)
    with pytest.raises(Conflict, match="head_changed"):
        journal.exchange("orphan:intent", dict(operation="submit"), before, first)
    assert journal.get("orphan:intent") is None
    assert journal.head() == first


def test_concurrent_advance_cannot_send_a_second_submission(research):
    entered, release = Event(), Event()

    class BlockingProvider(Provider):
        def submit(self, *args):
            entered.set()
            assert release.wait(10)
            return super().submit(*args)

    provider = BlockingProvider(response())
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(advance, research, provider)
        try:
            assert entered.wait(10)
            with pytest.raises(Conflict, match="run_busy"):
                advance(reopen(research), provider)
        finally:
            release.set()
        assert running.result(timeout=10)["state"] == "in_progress"
    assert len(provider.calls) == 1 and research.head()["submissions"] == 1


def test_legacy_archive_is_adopted_once_and_unchanged(research, monkeypatch):
    journal = research
    provider = Provider(*(response() for _ in range(21)))
    for _ in range(20):
        advance(journal, provider)
    with journal.repo.database.transaction() as tx:
        count = one(tx, "SELECT count(*) AS n FROM runtime_records WHERE project_id=:p", p=journal.project)[
            "n"
        ]
        query(
            tx,
            "DELETE FROM hosted_research_heads WHERE project_id=:p AND run_id=:r",
            p=journal.project,
            r=journal.run_id,
        )
    reopened = reopen(journal)
    assert status(reopened)["provider_job_id"] == "job/one"
    adopted = deepcopy(reopened.head())
    with journal.repo.database.read_transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM runtime_records WHERE project_id=:p", p=journal.project)["n"]
            == count
        )

    def no_adoption(value):
        raise AssertionError("legacy history was replayed twice")

    monkeypatch.setattr(reopened, "adopt_head", no_adoption)
    assert status(reopened)["state"] == "in_progress" and reopened.head() == adopted
    assert advance(reopened, provider)["state"] == "in_progress"


def test_head_rechecks_project_and_current_receipt_access(research, producer):
    journal = research
    advance(journal, Provider(response()))
    stranger = ResearchJournal(
        Repository(journal.repo.database, journal.repo.objects, uuid4()), journal.project, journal.run_id
    )
    with pytest.raises(AccessDenied):
        status(stranger)
    receipt = journal.get("exchange:1:receipt")
    anchor = journal.repo.create(
        journal.project,
        "Policy",
        dict(policy_kind="data", rules=dict(fixture=True)),
        producer=producer,
        operation_id=uuid4(),
    )
    event = DependencyLedger(journal.repo).invalidate(
        journal.project,
        anchor.ref,
        reason="Independent live receipt SHA denial fixture",
        category="quality",
    )
    with journal.repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO blob_access_revocations VALUES(:p,:sha,:event,'Head receipt denial')",
            p=journal.project,
            sha=receipt["raw_response_blob"]["sha256"],
            event=event,
        )
    with pytest.raises(AccessDenied):
        status(reopen(journal))
    with pytest.raises(AccessDenied):
        reopen(journal).bytes("exchange:1:receipt", receipt["raw_response_blob"])


def test_viewer_can_adopt_legacy_status_but_cannot_advance(research):
    journal = research
    advance(journal, Provider(response()))
    viewer = uuid4()
    with journal.repo.database.transaction() as tx:
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'viewer')", p=journal.project, a=viewer)
        query(
            tx,
            "DELETE FROM hosted_research_heads WHERE project_id=:p AND run_id=:r",
            p=journal.project,
            r=journal.run_id,
        )
    reader = ResearchJournal(
        Repository(journal.repo.database, journal.repo.objects, viewer), journal.project, journal.run_id
    )
    assert status(reader)["state"] == "in_progress"
    assert journal.head()["exchanges"] == 1
    provider = Provider()
    with pytest.raises(AccessDenied):
        advance(reader, provider)
    assert not provider.calls
