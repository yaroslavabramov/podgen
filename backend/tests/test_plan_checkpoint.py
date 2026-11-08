"""An operator can read the reviewed plan and continue that exact episode later."""

import json
from datetime import datetime, timedelta

import pytest

from podgen import writing_cli
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation import service
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.planning_continuation_cases import failed_source
from tests.test_editorial_independence import prepare
from tests.test_independent_readings import ReadingProvider
from tests.test_own_plan import own_dataset as own_dataset


def test_cli_plan_boundary_and_late_resume_share_the_clearance_and_never_repeat_a_call(
    own_dataset, tmp_path, monkeypatch
):
    prefix, fixed, options, _, _ = prepare(own_dataset, "gps-clock", tmp_path, monkeypatch)
    options.pop("review_mode")
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *_, **kwargs: provider)

    def call(action, options, *flags):
        args = [*prefix, action, *flags]
        for key, value in options.items():
            args.extend(("--" + key.replace("_", "-"), str(value)))
        return writing_cli.main(args)

    repo, project = own_dataset[:2]
    journal = EvaluationJournal(repo, project)
    assert call("run", fixed, "--stop-after-plan") == 2
    assert journal.run(fixed["run_id"]) is None
    assert call("run", options, "--stop-after-plan") == 0
    run_id = options["run_id"]
    directory = options["export_dir"]
    resume = dict(run_id=run_id, export_dir=directory)
    plan = json.loads((directory / "run.json").read_bytes())
    clearance = json.loads((directory / "planning-clearance.json").read_bytes())
    accepted = journal.read(RuntimeRef.model_validate(clearance["candidate_plan_ref"]))["output"]
    assert plan["status"] == "plan_ready" and plan["failure_code"] is None
    assert plan["accepted_plan_hash"] == digest(accepted) == clearance["candidate_plan_hash"]
    assert set(plan["invocations"]) == {"plan", "plan-review"}
    assert (directory / "generated-plan.md").is_file()
    assert not (directory / "review-result.md").exists() and not (directory / "draft.json").exists()
    assert journal.invocation(run_id, "draft") is None
    assert [body["metadata"]["podgen_stage"] for body in provider.requests] == ["plan", "plan-review"]

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=3)

    monkeypatch.setattr(service, "datetime", Later)
    before = (len(provider.requests), len(provider.counts))
    assert call("resume", resume, "--stop-after-plan") == 0
    assert call("run", options, "--stop-after-plan") == 0
    assert (len(provider.requests), len(provider.counts)) == before
    assert json.loads((directory / "run.json").read_bytes()) == plan
    with journal.lock(run_id):
        assert call("resume", resume) == 2
    assert journal.run(run_id)["status"] == "plan_ready"
    assert call("resume", resume) == 0
    assert [body["metadata"]["podgen_stage"] for body in provider.requests] == [
        "plan",
        "plan-review",
        "draft",
        "reader-review",
        "fact-review",
    ]
    draft = journal.invocation(run_id, "draft")
    frozen = journal.read(RuntimeRef(record_kind="tool_execution", record_id=draft["request_record_id"]))
    assert frozen["context"]["accepted_plan_hash"] == digest(accepted)
    assert frozen["context"]["accepted_plan_ref"] == plan["accepted_plan_ref"]
    assert frozen["context"]["planning_clearance_ref"] == plan["planning_clearance_ref"]
    assert call("resume", resume, "--stop-after-plan") == 2
    assert journal.run(run_id)["status"] == "completed_unaccepted" and len(provider.requests) == 5


def test_requested_stop_cannot_clear_a_genuine_editor_refusal(own_dataset, tmp_path, monkeypatch):
    source, journal, provider = failed_source(own_dataset, tmp_path, monkeypatch)
    before = len(provider.requests)
    with pytest.raises(RunFailure, match="planning_revision_limit_exhausted"):
        service.WritingEvaluation(own_dataset[0], lambda _: provider).run(source, stop_after_plan=True)
    assert len(provider.requests) == before
    assert journal.run(source.run_id)["status"] == "needs_attention"
    assert journal.invocation(source.run_id, "draft") is None
