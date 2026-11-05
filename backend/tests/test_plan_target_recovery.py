"""Explicit exact-address repair, preserving the paid plan and all independent judgments."""

import json
from copy import deepcopy

import pytest

from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.writing.planning_roles import PlanReviewOutput
from tests.credit_provider import CreditProvider
from tests.plan_target_provider import PlanTargetProvider
from tests.test_own_plan import command_for
from tests.test_own_plan import own_dataset as own_dataset


def execute(data, command, provider, **flags):
    return WritingEvaluation(data[0], lambda profile: provider, legacy_offline_transport=provider).run(
        command, **flags
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_repair_changes_only_invalid_targets_and_keeps_paid_plan_and_raw(
    own_dataset, legacy, tmp_path, monkeypatch
):
    repo, project, _, _, _ = own_dataset
    command = command_for(own_dataset)
    if legacy:
        create = EvaluationJournal.create_run

        def old_manifest(journal, command, manifest):
            copied = deepcopy(manifest)
            copied.pop("plan_review_target_catalog", None)
            copied.pop("plan_target_repair_instructions", None)
            return create(journal, command, copied)

        monkeypatch.setattr(EvaluationJournal, "create_run", old_manifest)
    provider = PlanTargetProvider()
    with pytest.raises(RunFailure, match="planning_output_validation_failed"):
        execute(own_dataset, command, provider)
    journal = EvaluationJournal(repo, project)
    original = journal.invocation(command.run_id, "plan-review")
    manifest = journal.manifest(command.run_id)
    raw = journal.last_response(original)
    request = journal.request(original)
    plan = journal.invocation(command.run_id, "plan")
    with pytest.raises(RunFailure):
        execute(own_dataset, command, provider)
    assert len(provider.requests) == 2
    result = execute(own_dataset, command, provider, repair_plan_targets=original["invocation_id"])
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 6
    assert journal.invocation(command.run_id, "plan") == plan
    assert journal.last_response(original) == raw
    assert journal.request(original) == request
    assert journal.manifest(command.run_id) == manifest
    assert sum(r["metadata"]["podgen_stage"] == "plan" for r in provider.requests) == 1
    attempts = journal.attempts(command.run_id, "plan-review")
    assert [row["attempt_kind"] for row in attempts] == ["role", "plan_target_repair"]
    review = journal.output(attempts[-1])["output"]
    source = json.loads(json.loads(raw)["output"][0]["content"][0]["text"])
    expected = deepcopy(source)
    expected["strengths"][1]["targets"][0] = "/plan/acts/0/title"
    expected["findings"][-1]["targets"][0] = "/limitations"
    assert review == expected
    parsed_plan = journal.output(plan)["output"]
    from podgen.writing.planning_roles import PlanningOutput

    PlanReviewOutput.model_validate(review).validate_against(
        PlanningOutput.model_validate(parsed_plan),
        material_locators=json.loads(request["input"][0]["content"][0]["text"])["author_materials"][
            "material_catalog"
        ],
    )
    retry_request = journal.request(attempts[-1])
    assert retry_request["input"][:-1] == request["input"]
    assert retry_request["instructions"].startswith(request["instructions"])
    assert retry_request["model"] == request["model"]
    assert execute(own_dataset, command, provider, repair_plan_targets=original["invocation_id"]) == result
    assert len(provider.requests) == 6
    exported = export_run(repo, project, command.run_id, tmp_path / "export")
    assert exported["additional_conditions"][0]["kind"] == "plan-target-repair-v1"
    assert (tmp_path / "export/archive/plan-review/attempt-1/request.json").is_file()
    assert "дополнительное условие" in (tmp_path / "export/generated-plan.md").read_text()


@pytest.mark.parametrize(
    "selection,code",
    [
        ("null", "unresolved_plan_review_target"),
        ("unknown", "unknown_plan_review_target_choice"),
        ("duplicate", "plan_target_repair_target_mismatch"),
        ("missing", "plan_target_repair_target_mismatch"),
        ("change-decision", "planning_output_validation_failed"),
    ],
)
def test_invalid_selection_stops_without_author_or_second_repair(own_dataset, selection, code):
    command = command_for(own_dataset)
    provider = PlanTargetProvider(selection=selection)
    with pytest.raises(RunFailure):
        execute(own_dataset, command, provider)
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    original = journal.invocation(command.run_id, "plan-review")
    with pytest.raises(RunFailure, match=code):
        execute(own_dataset, command, provider, repair_plan_targets=original["invocation_id"])
    stopped = journal.run(command.run_id)
    with pytest.raises(RunFailure, match="plan_target_repair_action_used"):
        execute(own_dataset, command, provider, repair_plan_targets=original["invocation_id"])
    assert journal.run(command.run_id) == stopped
    assert len(provider.requests) == 3
    assert journal.invocation(command.run_id, "draft") is None
    assert len(journal.attempts(command.run_id, "plan-review")) == 2


@pytest.mark.parametrize("credit_operation", ["role", "plan_target_repair"])
def test_target_repair_and_credit_recovery_share_one_bounded_semantic_attempt(own_dataset, credit_operation):
    command = command_for(own_dataset)
    provider = CreditProvider(PlanTargetProvider(), stage="plan-review", operation=credit_operation)
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    if credit_operation == "role":
        with pytest.raises(RunFailure, match="provider_http_429"):
            execute(own_dataset, command, provider)
        credit = journal.invocation(command.run_id, "plan-review")
        with pytest.raises(RunFailure, match="planning_output_validation_failed"):
            execute(own_dataset, command, provider, retry_credit_refusal=credit["invocation_id"])
        invalid = journal.invocation(command.run_id, "plan-review")
        result = execute(own_dataset, command, provider, repair_plan_targets=invalid["invocation_id"])
    else:
        with pytest.raises(RunFailure):
            execute(own_dataset, command, provider)
        invalid = journal.invocation(command.run_id, "plan-review")
        with pytest.raises(RunFailure, match="provider_http_429"):
            execute(own_dataset, command, provider, repair_plan_targets=invalid["invocation_id"])
        credit = journal.invocation(command.run_id, "plan-review")
        result = execute(own_dataset, command, provider, retry_credit_refusal=credit["invocation_id"])
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 7
    attempts = journal.attempts(command.run_id, "plan-review")
    assert len(attempts) == 3
    assert sum(row["attempt_kind"] == "plan_target_repair" for row in attempts) == 1
    assert execute(own_dataset, command, provider, repair_plan_targets=invalid["invocation_id"]) == result
    assert len(provider.requests) == 7


def test_prepared_selector_resume_does_not_reload_active_prompt_or_repeat_paid_stages(
    own_dataset, monkeypatch
):
    from podgen.runtime.evaluation import plan_target_recovery

    command = command_for(own_dataset)
    provider = PlanTargetProvider()
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    with pytest.raises(RunFailure):
        execute(own_dataset, command, provider)
    original = journal.invocation(command.run_id, "plan-review")

    def stop():
        if len(provider.requests) == 2:
            raise KeyboardInterrupt()

    provider.on_count = stop
    with pytest.raises(KeyboardInterrupt):
        execute(own_dataset, command, provider, repair_plan_targets=original["invocation_id"])
    child = journal.invocation(command.run_id, "plan-review")
    saved = journal.request(child)
    monkeypatch.setattr(
        plan_target_recovery,
        "load_plan_target_repair_instructions",
        lambda: pytest.fail("never re-read admitted selector prompt"),
    )
    provider.on_count = None
    result = execute(own_dataset, command, provider)
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 6
    assert provider.requests[2] == saved
    assert len(journal.attempts(command.run_id, "plan-review")) == 2


def test_repair_of_second_review_binds_the_revised_plan_not_the_initial_one(own_dataset):
    command = command_for(own_dataset)
    provider = PlanTargetProvider(stage="plan-review-revised", decisions=("revise", "accept"))
    with pytest.raises(RunFailure):
        execute(own_dataset, command, provider)
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    original = journal.invocation(command.run_id, "plan-review-revised")
    revised = journal.invocation(command.run_id, "plan-revise")
    result = execute(own_dataset, command, provider, repair_plan_targets=original["invocation_id"])
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 8
    writer = journal.request(journal.invocation(command.run_id, "draft"))
    payload = json.loads(writer["input"][0]["content"][0]["text"])
    assert payload["accepted_plan_ref"]["record_id"] == str(revised["output_record_id"])
    assert payload["accepted_plan"] == journal.output(revised)["output"]


def test_pinned_selector_definition_is_reused_after_action_interruption(own_dataset, monkeypatch):
    from uuid import uuid5

    from podgen.runtime.evaluation import plan_target_recovery

    command = command_for(own_dataset)
    provider = PlanTargetProvider()
    with pytest.raises(RunFailure):
        execute(own_dataset, command, provider)
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    original = journal.invocation(command.run_id, "plan-review")
    child_id = uuid5(original["invocation_id"], "plan-target-repair:1")
    first = plan_target_recovery.pin_repair_prompt(journal, command.run_id, child_id)
    monkeypatch.setattr(
        plan_target_recovery,
        "load_plan_target_repair_instructions",
        lambda: pytest.fail("use the already pinned definition"),
    )
    assert plan_target_recovery.pin_repair_prompt(journal, command.run_id, child_id) == first
    result = execute(own_dataset, command, provider, repair_plan_targets=original["invocation_id"])
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 6
