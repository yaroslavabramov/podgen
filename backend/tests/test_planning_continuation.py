"""A real native descendant pays only for new work and never bypasses plan acceptance."""

import json
from copy import deepcopy
from threading import Event, Thread
from uuid import uuid4

import pytest

from podgen import writing_cli
from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict, ContractError
from podgen.research_writing_cli import main as research_main
from podgen.runtime.evaluation.contracts import RunFailure, command_payload
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.planning import verify_clearance
from podgen.runtime.evaluation.planning_continuation.cli import prepare
from podgen.runtime.evaluation.planning_continuation.source import build_command, continuation_id
from podgen.runtime.evaluation.service import WritingEvaluation
from tests.planning_continuation_cases import ContinuationProvider, failed_source
from tests.stage_pause import pause_before_count
from tests.test_editorial_independence import payload
from tests.test_own_plan import own_dataset as own_dataset


def test_native_continuation_preview_accept_writing_export_and_resume(own_dataset, tmp_path, monkeypatch):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    before = deepcopy(journal.manifest(source.run_id))
    provider = ContinuationProvider("accept")
    child = build_command(journal, source.run_id, limit=2)
    assert child.run_id == continuation_id(source.run_id)
    folder = tmp_path / "continued"
    result = prepare(own_dataset[0], child, folder)
    assert result["endpoint_called"] is False and not provider.requests
    preview = json.loads((folder / "plan-revise-preview.json").read_text())["request"]
    frozen = journal.manifest(child.run_id)
    seed = json.loads((folder / "planning-seed-plan.json").read_text())
    review = json.loads((folder / "planning-seed-review.json").read_text())
    assert payload(preview)["candidate_plan"]["plan"]["route_summary"] == seed["plan"]["route_summary"]
    assert payload(preview)["candidate_plan"]["plan"]["ending"] == seed["plan"]["ending"]
    assert set(seed["plan"]["ending"]) == {"last_moment", "why_end_here", "promises_to_close"}
    assert payload(preview)["revision_guidance"]["decision_reason"] == review["decision_reason"]
    assert frozen["materials_blob"] == before["materials_blob"]
    assert frozen["prompt_refs"]["plan-revise"] == before["prompt_refs"]["plan-revise"]
    service = WritingEvaluation(own_dataset[0], lambda _: provider)
    assert service.run(child)["status"] == "completed_unaccepted"
    stages = [r["metadata"]["podgen_stage"] for r in provider.requests]
    assert stages == ["plan-revise", "plan-review-revised", "draft", "reader-review", "fact-review"]
    assert provider.requests[0] == preview
    revised = journal.output(journal.invocation(child.run_id, "plan-revise"))["output"]
    assert revised["plan"]["ending"] == seed["plan"]["ending"]
    assert service.run(child)["status"] == "completed_unaccepted"
    assert len(provider.requests) == 5
    assert journal.manifest(source.run_id) == before
    assert journal.run(source.run_id)["failure_code"] == "planning_revision_limit_exhausted"
    proof = verify_clearance(journal, child.run_id)
    assert proof["executed_stages"] == ["plan-revise", "plan-review-revised"]
    result = export_run(own_dataset[0], source.project_id, child.run_id, folder)
    assert "plan" not in result["invocations"] and "plan-review" not in result["invocations"]
    assert result["input_tokens"] == 500
    assert (folder / "planning-clearance.json").is_file()
    assert result["planning_continuation"]["depth"] == 1


def test_two_bounded_descendants_and_another_refusal_do_not_start_the_writer(
    own_dataset, tmp_path, monkeypatch
):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    provider = ContinuationProvider()
    service = WritingEvaluation(own_dataset[0], lambda _: provider)
    first = build_command(journal, source.run_id, limit=2)
    with pytest.raises(RunFailure, match="planning_revision_limit_exhausted"):
        service.run(first)
    second = build_command(journal, first.run_id)
    assert second.planning_continuation_limit == 2
    with pytest.raises(RunFailure, match="planning_revision_limit_exhausted"):
        service.run(second)
    with pytest.raises(ContractError, match="planning_continuation_limit_exhausted"):
        build_command(journal, second.run_id)
    with pytest.raises(ContractError, match="planning_continuation_limit_changed"):
        build_command(journal, first.run_id, limit=3)
    assert [r["metadata"]["podgen_stage"] for r in provider.requests] == [
        "plan-revise",
        "plan-review-revised",
    ] * 2


@pytest.mark.parametrize("decision,research", [("accept", True), ("needs_research", False)])
def test_research_is_a_honest_terminal_result(own_dataset, tmp_path, monkeypatch, decision, research):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    child = build_command(journal, source.run_id, limit=2)
    provider = ContinuationProvider(decision, research=research)
    with pytest.raises(RunFailure, match="planning_needs_research"):
        WritingEvaluation(own_dataset[0], lambda _: provider).run(child)
    assert not any(r["metadata"]["podgen_stage"] == "draft" for r in provider.requests)
    with pytest.raises(ContractError, match="planning_continuation_requires_exhausted_run"):
        build_command(journal, child.run_id)


def test_resume_keeps_completed_revision_and_pinned_request(own_dataset, tmp_path, monkeypatch):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    child = build_command(journal, source.run_id, limit=2)
    provider = ContinuationProvider("accept")
    service = WritingEvaluation(own_dataset[0], lambda _: provider)
    with pause_before_count(child.run_id, "plan-review-revised"):
        service.run(child)
    pending = journal.request(journal.invocation(child.run_id, "plan-review-revised"))

    assert service.run(child)["status"] == "completed_unaccepted"
    assert provider.requests[1] == pending
    assert [r["metadata"]["podgen_stage"] for r in provider.requests].count("plan-revise") == 1


def test_continuation_identity_and_concurrency_prevent_duplicate_paid_descendants(
    own_dataset, tmp_path, monkeypatch
):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    one = build_command(journal, source.run_id, limit=2)
    assert one == build_command(journal, source.run_id, limit=2)
    with pytest.raises(ContractError, match="planning_continuation_run_id_mismatch"):
        build_command(journal, source.run_id, uuid4(), limit=2)
    entered, release = Event(), Event()
    errors = []

    def block(stage):
        if stage == "plan-revise":
            entered.set()
            assert release.wait(120)

    provider = ContinuationProvider("accept", on_plan=block)
    service = WritingEvaluation(own_dataset[0], lambda _: provider)

    def run():
        try:
            service.run(one)
        except Exception as error:
            errors.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        assert entered.wait(120)
        with pytest.raises(Conflict, match="evaluation_run_busy"):
            service.run(build_command(journal, source.run_id, limit=2))
    finally:
        release.set()
        # Completion includes real archive/ACL work; this is a coordination timeout, not a run budget.
        worker.join(120)
    assert not worker.is_alive() and not errors
    assert [r["metadata"]["podgen_stage"] for r in provider.requests].count("plan-revise") == 1


def test_old_command_fingerprint_does_not_gain_continuation_fields(own_dataset, tmp_path, monkeypatch):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    payload = command_payload(source)
    assert "planning_parent_run_id" not in payload and "planning_continuation_limit" not in payload
    assert digest(payload) == journal.run(source.run_id)["input_fingerprint"]


def test_ordinary_cli_prepares_runs_and_resumes_the_same_descendant(own_dataset, tmp_path, monkeypatch):
    source, _, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    provider = ContinuationProvider("accept")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **kw: provider)
    prefix = [
        "--project",
        str(source.project_id),
        "--actor",
        str(source.actor_id),
        "--objects",
        str(own_dataset[0].objects.root),
    ]
    options = [
        "continue-planning",
        "--source-run-id",
        str(source.run_id),
        "--max-additional-plan-revisions",
        "2",
        "--export-dir",
        str(tmp_path / "cli-child"),
    ]
    assert research_main([*prefix, *options, "--preview-only"]) == 0
    assert not provider.requests and not provider.counts
    assert research_main([*prefix, *options, "--stop-after-plan"]) == 0
    assert [r["metadata"]["podgen_stage"] for r in provider.requests] == [
        "plan-revise",
        "plan-review-revised",
    ]
    assert json.loads((tmp_path / "cli-child/run.json").read_bytes())["status"] == "plan_ready"
    assert (
        research_main(
            [
                *prefix,
                "resume",
                "--run-id",
                str(continuation_id(source.run_id)),
                "--export-dir",
                str(tmp_path / "cli-child"),
            ]
        )
        == 0
    )
    assert len(provider.requests) == 5


@pytest.mark.parametrize("version", ["editorial-role-projections/8", "editorial-role-projections/9"])
def test_continuation_provider_emits_the_exact_pinned_planner_shape(version):
    from podgen.runtime.evaluation.role_schema import response_schema
    from podgen.writing.planning_versions import planning_model

    visible = dict(material_catalog=["test-material"], assignment=dict(speakers=[dict(label="NARRATOR")]))
    body = dict(
        model="local-test-only",
        metadata=dict(podgen_stage="plan"),
        input=[dict(role="user", content=[dict(type="input_text", text=json.dumps(visible))])],
        text=dict(
            format=dict(
                schema=response_schema("plan", "editorial-role-packets/1", projection_version=version)
            )
        ),
    )
    response = json.loads(ContinuationProvider().submit(body, client_request_id="local-test-only").body)
    result = json.loads(response["output"][0]["content"][0]["text"])
    parsed = planning_model(version).model_validate(result)
    parsed.validate_against(material_locators=visible["material_catalog"], speaker_ids=["NARRATOR"])
    assert parsed.model_dump(mode="json") == result
    assert ("last_moment" in result["plan"]["ending"]) == version.endswith("/9")
    assert ("answer_to_deliver" in result["plan"]["ending"]) == version.endswith("/8")
