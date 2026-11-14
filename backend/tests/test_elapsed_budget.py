"""Explicit elapsed extensions preserve frozen inputs, paid work and other budgets."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import service
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.elapsed_budget import ElapsedBudget
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.writing_cli import parser
from tests.test_writing_evaluation import FakeProvider
from tests.test_writing_evaluation import setup_writer as setup_writer


@pytest.fixture
def expired(repo, setup_writer, monkeypatch):
    command, loader, _ = setup_writer
    provider = FakeProvider()
    started = datetime.now(timezone.utc)
    seconds = [0]

    class Clock:
        @staticmethod
        def now(tz):
            return started + timedelta(seconds=seconds[0])

    monkeypatch.setattr(service, "datetime", Clock)

    def expire_after_count():
        seconds[0] = command.limits.max_elapsed_seconds + 1

    provider.on_count = expire_after_count
    runner = service.WritingEvaluation(
        repo, lambda _: provider, materials_loader=loader, legacy_offline_transport=provider
    )
    with pytest.raises(RunFailure, match="run_elapsed_budget_exceeded"):
        runner.run(command)
    provider.on_count = None
    journal = EvaluationJournal(repo, command.project_id)
    frozen = journal.manifest(command.run_id)
    assert len(provider.counts) == 1 and not provider.requests
    return runner, command, provider, journal, frozen


def test_resume_without_elapsed_extension_still_blocks(expired):
    runner, command, provider, journal, frozen = expired
    with pytest.raises(RunFailure, match="run_elapsed_budget_exceeded"):
        runner.run(command)
    assert len(provider.counts) == 1 and not provider.requests
    assert journal.manifest(command.run_id) == frozen


def test_absolute_extension_resumes_and_repeats_without_adding_time(expired, tmp_path):
    runner, command, provider, journal, frozen = expired
    ceiling = command.limits.max_elapsed_seconds + 100
    with journal.lock(command.run_id):
        initial = ElapsedBudget.load(journal, frozen, requested=ceiling)
    result = runner.run(command, max_elapsed_seconds=ceiling)
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 3 and len(provider.counts) == 3
    assert journal.manifest(command.run_id) == frozen
    assert command.limits.model_dump() == frozen["command"]["limits"]
    assert runner.run(command, max_elapsed_seconds=ceiling) == result
    restored = ElapsedBudget.load(journal, frozen)
    assert restored == initial and len(restored.authorizations) == 1
    with pytest.raises(ContractError, match="elapsed_allowance_invalid"):
        runner.run(command, max_elapsed_seconds=ceiling - 1)
    exported = export_run(journal.repo, command.project_id, command.run_id, tmp_path / "export")
    assert exported["authorized_max_elapsed_seconds"] == ceiling
    assert json.loads((tmp_path / "export/elapsed-authorizations.json").read_text()) == list(
        initial.authorizations
    )
    assert json.loads((tmp_path / "export/manifest.json").read_text()) == frozen
    assert len(provider.requests) == 3 and len(provider.counts) == 3


@pytest.mark.parametrize("value", [True, 1.5, 0, -1, 7200, 7199, 2**63])
def test_invalid_or_nonincreasing_elapsed_ceiling_is_rejected(expired, value):
    runner, command, provider, journal, frozen = expired
    with pytest.raises(ContractError, match="elapsed_allowance_invalid"):
        runner.run(command, max_elapsed_seconds=value)
    assert not ElapsedBudget.load(journal, frozen).authorizations
    assert not provider.requests and len(provider.counts) == 1


def test_elapsed_extension_is_an_explicit_resume_option():
    args = parser().parse_args(
        [
            "--project",
            str(uuid4()),
            "--actor",
            str(uuid4()),
            "--objects",
            "/tmp/objects",
            "resume",
            "--run-id",
            str(uuid4()),
            "--export-dir",
            "/tmp/export",
            "--max-elapsed-seconds",
            "98000",
        ]
    )
    assert args.max_elapsed_seconds == 98000


def test_elapsed_lookup_preserves_unrelated_raw_nul_unicode_and_uses_grant_index(expired):
    from podgen.persistence.database import json_text, query

    _, command, _, journal, frozen = expired
    ceiling = command.limits.max_elapsed_seconds + 100
    with journal.lock(command.run_id):
        expected = ElapsedBudget.load(journal, frozen, requested=ceiling)
    identity = uuid4()
    payload = dict(full_text="Сохранить\x00原文 🌍", nested=dict(value="\x00"))
    raw = json_text(payload)
    with journal.repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
            p=journal.project,
            r=identity,
            body=raw,
        )
        assert (
            query(
                tx,
                "SELECT payload FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=journal.project,
                r=identity,
            ).scalar_one()
            == payload
        )
        assert (
            query(
                tx,
                "SELECT payload::text FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=journal.project,
                r=identity,
            ).scalar_one()
            == raw
        )
        tx.exec_driver_sql("SET LOCAL enable_seqscan=off")
        plan = query(
            tx,
            """EXPLAIN (FORMAT JSON) SELECT record_id FROM runtime_records
            WHERE project_id=:p AND record_kind='tool_execution'
              AND podgen_runtime_reference_document(payload)->>'contract'='resume-elapsed-authorization/1'
              AND podgen_runtime_reference_document(payload)->>'run_id'=:run""",
            p=journal.project,
            run=str(command.run_id),
        ).scalar_one()
        assert "runtime_elapsed_authorizations_by_run" in json.dumps(plan)
    assert ElapsedBudget.load(journal, frozen) == expected
