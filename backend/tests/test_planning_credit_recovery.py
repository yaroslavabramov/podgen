"""The ordinary resume CLI recovers plan review without recreating the already paid proposal."""

import json

import pytest
from podgen import writing_cli
from podgen.research_writing_cli import main as research_main
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation

from tests.credit_provider import CreditProvider
from tests.own_plan_provider import OwnPlanProvider
from tests.test_own_plan import command_for
from tests.test_own_plan import own_dataset as own_dataset


@pytest.mark.parametrize("stage", ["plan-review", "plan-review-revised"])
def test_cli_planning_retry_keeps_paid_plan_and_exact_frozen_inputs(
    own_dataset, tmp_path, monkeypatch, stage
):
    repo, project, _, _, _ = own_dataset
    command = command_for(own_dataset, case="snow-pump")
    provider = CreditProvider(
        OwnPlanProvider(decisions=("accept", "accept") if stage == "plan-review" else ("revise", "accept")),
        stage=stage,
    )
    with pytest.raises(RunFailure, match="provider_http_429"):
        WritingEvaluation(repo, lambda profile: provider, legacy_offline_transport=provider).run(command)
    journal = EvaluationJournal(repo, project)
    manifest = journal.manifest(command.run_id)
    plan = journal.invocation(command.run_id, "plan")
    plan_record = journal.output(plan)
    target = journal.invocation(command.run_id, stage)
    original = journal.request(target)
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(
        writing_cli,
        "WritingEvaluation",
        lambda repo, factory: WritingEvaluation(repo, factory, legacy_offline_transport=provider),
    )
    args = [
        "--project",
        str(project),
        "--actor",
        str(repo.actor_id),
        "--objects",
        str(repo.objects.root),
        "resume",
        "--run-id",
        str(command.run_id),
        "--retry-credit-refusal",
        str(target["invocation_id"]),
        "--export-dir",
        str(tmp_path / "export"),
    ]
    assert research_main(args) == 0
    calls = len(provider.requests)
    assert research_main(args) == 0
    assert len(provider.requests) == calls
    assert sum(r["metadata"]["podgen_stage"] == "plan" for r in provider.requests) == 1
    assert journal.output(plan) == plan_record
    assert journal.invocation(command.run_id, "plan") == plan
    assert journal.manifest(command.run_id) == manifest
    latest = journal.invocation(command.run_id, stage)
    retried = journal.request(latest)
    assert {key: value for key, value in retried.items() if key != "metadata"} == {
        key: value for key, value in original.items() if key != "metadata"
    }
    exported = json.loads((tmp_path / "export/run.json").read_text())
    assert len(exported["invocations"][stage]["attempts"]) == 2
    assert not exported["unknown_usage_invocations"]
    assert (tmp_path / f"export/archive/{stage}/attempt-1/request.json").is_file()
    assert (tmp_path / "export/generated-plan.md").is_file()
