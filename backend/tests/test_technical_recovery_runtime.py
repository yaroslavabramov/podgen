"""Ordinary resume, real SQL lineage and locks, local providers only. Run serially with the DB suite."""

import json
from copy import deepcopy

import pytest
from podgen import writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.contracts import RunFailure, RunLimits, WritingRunCommand
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import ProviderFault, WireReply

from tests.test_editorial_independence import invoke, prepare
from tests.test_independent_readings import ReadingProvider
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_writing_evaluation import FakeProvider
from tests.test_writing_evaluation import setup_writer as setup_writer


class LostFact(ReadingProvider):
    def __init__(self):
        super().__init__()
        self.lost = False

    def submit(self, body, *, client_request_id):
        if body["metadata"]["podgen_stage"] == "fact-review" and not self.lost:
            self.lost = True
            self.requests.append(deepcopy(body))
            raise ProviderFault(
                "provider_http_520", ambiguous=True, reply=WireReply(b"upstream timeout", 520)
            )
        return super().submit(body, client_request_id=client_request_id)


def test_ordinary_resume_skips_unused_planning_stages_and_retains_paid_readings(
    own_dataset, tmp_path, monkeypatch
):
    prefix, _, options, _, _ = prepare(own_dataset, "gps-clock", tmp_path, monkeypatch)
    options.pop("review_mode")
    provider = LostFact()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 2
    repo, project, *_ = own_dataset
    journal = EvaluationJournal(repo, project)
    run = options["run_id"]
    saved_manifest = journal.manifest(run)
    assert journal.invocation(run, "plan-revise") is None
    assert journal.invocation(run, "plan-review-revised") is None
    preserved = {
        stage: journal.invocation(run, stage) for stage in ("plan", "plan-review", "draft", "reader-review")
    }
    original = journal.invocation(run, "fact-review")
    assert original["status"] == "outcome_unknown"
    # The standard product resume needs neither a research option nor an invocation UUID.
    args = [*prefix, "resume", "--run-id", str(run), "--export-dir", str(tmp_path / "export")]
    assert writing_cli.main(args) == 0
    sent = len(provider.requests)
    assert writing_cli.main(args) == 0 and len(provider.requests) == sent
    assert [r["metadata"]["podgen_stage"] for r in provider.requests] == [
        "plan",
        "plan-review",
        "draft",
        "reader-review",
        "fact-review",
        "fact-review",
    ]
    assert journal.manifest(run) == saved_manifest
    assert all(journal.invocation(run, stage) == row for stage, row in preserved.items())
    attempts = journal.attempts(run, "fact-review")
    assert [row["status"] for row in attempts] == ["outcome_unknown", "completed"]
    assert [row["attempt_kind"] for row in attempts] == ["role", "technical_retry"]
    exported = export_run(repo, project, run, tmp_path / "export")
    assert str(original["invocation_id"]) in exported["unknown_usage_invocations"]
    assert (tmp_path / "export/archive/fact-review/attempt-1/request-record.json").is_file()
    assert not any(r["metadata"]["podgen_stage"].startswith("revise") for r in provider.requests)


def test_competing_resume_cannot_create_or_submit_replacement(repo, setup_writer):
    command, loader, _ = setup_writer
    command = command.model_copy(update=dict(limits=RunLimits(max_total_output_tokens=300000)))
    provider = FakeProvider(fault="timeout")
    runner = WritingEvaluation(
        repo, lambda _: provider, materials_loader=loader, legacy_offline_transport=provider
    )
    with pytest.raises(RunFailure):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    with journal.lock(command.run_id):
        with pytest.raises(Conflict, match="evaluation_run_busy"):
            runner.run(command)
    assert len(provider.requests) == len(journal.attempts(command.run_id, "draft")) == 1
    provider.fault = None
    assert runner.run(command)["status"] == "completed_unaccepted"
    count = len(provider.requests)
    assert (
        runner.run(WritingRunCommand.model_validate(journal.manifest(command.run_id)["command"]))["status"]
        == "completed_unaccepted"
    )
    assert len(provider.requests) == count


def test_ordinary_resume_cap_updates_run_and_export_failure(repo, setup_writer, tmp_path):
    command, loader, _ = setup_writer
    command = command.model_copy(update=dict(limits=RunLimits(max_total_output_tokens=300000)))
    provider = FakeProvider(fault="timeout")
    runner = WritingEvaluation(
        repo, lambda _: provider, materials_loader=loader, legacy_offline_transport=provider
    )
    for _ in range(3):
        with pytest.raises(RunFailure, match="provider_transport_outcome_unknown"):
            runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    for _ in range(2):
        with pytest.raises(RunFailure, match="technical_recovery_limit_exhausted"):
            runner.run(command)
    assert journal.run(command.run_id)["failure_code"] == "technical_recovery_limit_exhausted"
    assert len(journal.attempts(command.run_id, "draft")) == len(provider.requests) == 3
    exported = export_run(repo, command.project_id, command.run_id, tmp_path / "export")
    assert exported["failure_code"] == "technical_recovery_limit_exhausted"
    assert len(exported["unknown_usage_invocations"]) == 3


def test_transient_retrieve_failure_updates_run_without_replacement(repo, setup_writer):
    class Known(FakeProvider):
        def submit(self, body, *, client_request_id):
            reply = super().submit(body, client_request_id=client_request_id)
            response = json.loads(reply.body)
            response["status"] = "in_progress"
            return WireReply(json.dumps(response).encode())

        def retrieve(self, response_id):
            raise ProviderFault("provider_http_503")

    command, loader, _ = setup_writer
    provider = Known()
    runner = WritingEvaluation(
        repo, lambda _: provider, materials_loader=loader, legacy_offline_transport=provider
    )
    with pytest.raises(RunFailure, match="response_not_retrievable"):
        runner.run(command)
    with pytest.raises(RunFailure, match="provider_http_503"):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    assert journal.run(command.run_id)["failure_code"] == "provider_http_503"
    assert len(provider.requests) == len(journal.attempts(command.run_id, "draft")) == 1
    assert journal.invocation(command.run_id, "draft")["status"] == "outcome_unknown"
