"""Real frozen sources, real journal/ACLs and ordinary CLI, with controlled offline plan decisions."""

import json
from uuid import uuid4

import pytest
from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.database import query
from podgen.persistence.editorial_staging import EditorialStaging
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.research_writing_cli import main as research_main
from podgen.runtime.evaluation.contracts import RunFailure, RunLimits, WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.writing.materials import load_own_plan_materials

from tests.own_plan_provider import OwnPlanProvider
from tests.test_writing_cli import ROOT


@pytest.fixture(scope="module")
def own_dataset(database, tmp_path_factory):
    repo = Repository(
        database, FileObjectStore(tmp_path_factory.mktemp("own-plan-blobs")), uuid4(), purpose="evaluation"
    )
    project = uuid4()
    with database.transaction() as tx:
        query(tx, "INSERT INTO projects VALUES(:p,'tenant')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=repo.actor_id)
    report = EditorialStaging(repo).import_directory(project, ROOT)
    writer = create_profile(
        repo, project, model="synthetic-pinned-snapshot", context_limit=1050000, output_limit=32768
    )
    planner = create_profile(
        repo,
        project,
        model="synthetic-pinned-snapshot",
        context_limit=1050000,
        output_limit=32768,
        roles=["planner", "plan_reviewer"],
    )
    return repo, project, report, writer.ref, planner.ref


def command_for(data, case="gps-clock", **changes):
    repo, project, report, writer, planner = data
    changes.setdefault("behavior_profile", "existing/1")
    return WritingRunCommand(
        input_contract="legacy-author-materials/1",
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=case,
        author_ref=RuntimeRef.model_validate(report["author_inputs"][case]),
        writer_profile_ref=writer,
        reviewer_profile_ref=writer,
        planner_profile_ref=planner,
        plan_reviewer_profile_ref=planner,
        plan_mode="own-plan",
        **changes,
    )


def execute(data, command, provider):
    return WritingEvaluation(data[0], lambda profile: provider, legacy_offline_transport=provider).run(
        command
    )


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_ordinary_cli_new_plan_revision_review_exact_delivery_and_export(
    own_dataset, case, tmp_path, monkeypatch
):
    repo, project, report, writer, planner = own_dataset
    provider = OwnPlanProvider()
    command = command_for(own_dataset, case)
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    from podgen.runtime.evaluation.contracts import WritingRunCommand as RunCommand
    from podgen.runtime.evaluation.service import WritingEvaluation as Evaluation

    class OfflineLegacyCommand(RunCommand):
        input_contract: str = "legacy-author-materials/1"

    monkeypatch.setattr(writing_cli, "WritingRunCommand", OfflineLegacyCommand)
    monkeypatch.setattr(
        writing_cli,
        "WritingEvaluation",
        lambda repo, factory: Evaluation(repo, factory, legacy_offline_transport=provider),
    )
    args = [
        "--project",
        str(project),
        "--actor",
        str(repo.actor_id),
        "--objects",
        str(repo.objects.root),
        "run",
        "--review-mode",
        "informed-v1",
        "--run-id",
        str(command.run_id),
        "--case",
        case,
        "--author-ref",
        json.dumps(report["author_inputs"][case]),
        "--writer-profile",
        json.dumps(writer.model_dump(mode="json")),
        "--reviewer-profile",
        json.dumps(writer.model_dump(mode="json")),
        "--plan-mode",
        "own-plan",
        "--planner-profile",
        json.dumps(planner.model_dump(mode="json")),
        "--plan-reviewer-profile",
        json.dumps(planner.model_dump(mode="json")),
        "--export-dir",
        str(tmp_path / "export"),
    ]
    assert research_main(args) == 0
    assert research_main(args) == 0
    assert [r["metadata"]["podgen_stage"] for r in provider.requests] == [
        "plan",
        "plan-review",
        "plan-revise",
        "plan-review-revised",
        "draft",
        "review",
        "revise",
    ]
    journal = EvaluationJournal(repo, project)
    revised = journal.output(journal.invocation(command.run_id, "plan-revise"))["output"]
    payloads = [json.loads(r["input"][0]["content"][0]["text"]) for r in provider.requests]
    assert payloads[1]["candidate_plan"] != revised
    assert payloads[3]["candidate_plan"] == revised
    for payload in payloads:
        materials = payload["author_materials"]
        assert materials["plan_mode"] == "own-plan"
        assert "fixed-plan" in materials["provided_plan_instructions"]
        assert all(
            d["path"] != f"author/{case}/plan.md" and "evaluator-only" not in d["path"]
            for d in materials["documents"]
        )
        assert not any("/plan.md" in ref for ref in materials["material_catalog"])
    for payload in payloads[4:]:
        assert payload["accepted_plan"] == revised
        assert payload["accepted_plan_ref"] == payloads[3]["candidate_plan_ref"]
    exported = json.loads((tmp_path / "export/run.json").read_text())
    assert exported["input_tokens"] == 700 and exported["output_tokens_known"] == 861
    assert (tmp_path / "export/plan-review-revised.json").is_file()
    assert (tmp_path / "export/planning-clearance.json").is_file()
    readable = (tmp_path / "export/generated-plan.md").read_text()
    assert "допустил эту версию к автору" in readable
    assert "SYNTHETIC OWN PLAN plan-revise" in readable
    assert "SYNTHETIC OWN PLAN plan:" not in readable
    assert "SYNTHETIC OWN PLAN plan-revise" in (tmp_path / "export/plan-revise.json").read_text()


@pytest.mark.parametrize(
    "deficit,decisions,limit,expected_calls,code",
    [
        (True, ("accept", "accept"), 1, 1, "planning_needs_research"),
        (False, ("needs_research", "accept"), 1, 2, "planning_needs_research"),
        (False, ("revise", "accept"), 0, 2, "planning_revision_limit_exhausted"),
        (False, ("revise", "revise"), 1, 4, "planning_revision_limit_exhausted"),
    ],
)
def test_planning_blocks_preserve_outputs_and_never_start_writer(
    own_dataset, deficit, decisions, limit, expected_calls, code
):
    command = command_for(own_dataset, max_plan_revisions=limit)
    provider = OwnPlanProvider(plan_deficit=deficit, decisions=decisions)
    for _ in range(2):
        with pytest.raises(RunFailure, match=code):
            execute(own_dataset, command, provider)
    assert len(provider.requests) == expected_calls
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    assert journal.run(command.run_id)["status"] == "needs_attention"
    assert journal.invocation(command.run_id, "draft") is None
    if deficit:
        assert journal.output(journal.invocation(command.run_id, "plan"))["output"]["plan"] is None


@pytest.mark.parametrize(
    "stage,fault",
    [
        ("plan", "timeout"),
        ("plan", "locator"),
        ("plan-review", "pointer"),
        ("plan-review", "accept-blocked"),
        ("plan-revise", "incomplete"),
        ("plan-review-revised", "model"),
        ("plan-review", "invalid-json"),
    ],
)
def test_planning_faults_only_replace_technical_attempts_and_never_start_writer(own_dataset, stage, fault):
    command = command_for(own_dataset)
    provider = OwnPlanProvider(
        decisions=("accept", "accept") if fault == "accept-blocked" else ("revise", "accept"),
        fault_stage=stage,
        plan_fault=fault,
    )
    for _ in range(2):
        with pytest.raises(RunFailure):
            execute(own_dataset, command, provider)
    stages = [r["metadata"]["podgen_stage"] for r in provider.requests]
    assert stages.count(stage) == (2 if fault == "timeout" else 1)
    assert "draft" not in stages


def test_nonblocking_uncertainty_is_not_a_research_gate(own_dataset):
    provider = OwnPlanProvider(decisions=("accept", "accept"), nonblocking=True)
    execute(own_dataset, command_for(own_dataset), provider)
    assert len(provider.requests) == 5


def test_selector_never_opens_fixed_plan_and_ref_aliases_have_no_fallback(own_dataset, monkeypatch):
    repo, project, report, _, _ = own_dataset
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    forbidden = {
        RuntimeRef.model_validate(ref).record_id
        for path, ref in report["file_refs"].items()
        if path.endswith("/plan.md") or path.startswith("evaluator-only/")
    }
    original = generation.get_runtime

    def checked(project, ref):
        assert ref.record_id not in forbidden
        return original(project, ref)

    monkeypatch.setattr(generation, "get_runtime", checked)
    command = command_for(own_dataset)
    materials = load_own_plan_materials(generation, project, command.author_ref, case_id=command.case_id)
    assert len(materials.documents) == 7
    alias = RuntimeRef(record_kind="user_input", record_id=uuid4())
    broad = repo.get_runtime(project, command.author_ref)
    repo.append_runtime(
        project, alias, dict(broad, nested_alias=report["file_refs"]["author/gps-clock/plan.md"])
    )
    with pytest.raises(AccessDenied):
        load_own_plan_materials(generation, project, alias, case_id=command.case_id)


def test_partial_resume_uses_exact_saved_plan_and_pinned_prompts(own_dataset, monkeypatch):
    from podgen.runtime.evaluation import manifest as manifest_module

    command = command_for(own_dataset)
    provider = OwnPlanProvider()

    def interrupt():
        if len(provider.requests) == 3:
            raise KeyboardInterrupt()

    provider.on_count = interrupt
    with pytest.raises(KeyboardInterrupt):
        execute(own_dataset, command, provider)
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    saved = journal.request(journal.invocation(command.run_id, "plan-review-revised"))
    manifest = journal.manifest(command.run_id)
    provider.on_count = None
    monkeypatch.setattr(
        manifest_module,
        "resolve_behavior",
        lambda *args, **kwargs: pytest.fail("resume must not read active prompts"),
    )
    result = execute(own_dataset, command, provider)
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 7
    assert provider.requests[3] == saved
    assert journal.manifest(command.run_id) == manifest
    assert execute(own_dataset, command, provider) == result
    assert len(provider.requests) == 7


def test_all_planning_calls_charge_the_same_run_budget(own_dataset):
    command = command_for(own_dataset, limits=RunLimits(max_total_input_tokens=250))
    provider = OwnPlanProvider()
    with pytest.raises(RunFailure, match="run_token_budget_exceeded"):
        execute(own_dataset, command, provider)
    assert len(provider.requests) == 2
    assert len(provider.counts) == 3


def test_lost_planning_acknowledgement_resumes_through_a_linked_replacement(own_dataset):
    command = command_for(own_dataset)
    provider = OwnPlanProvider()

    def crash():
        if len(provider.requests) == 3:
            raise KeyboardInterrupt()

    provider.on_submit = crash
    with pytest.raises(KeyboardInterrupt):
        execute(own_dataset, command, provider)
    provider.on_submit = None
    assert execute(own_dataset, command, provider)["status"] == "completed_unaccepted"
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    attempts = journal.attempts(command.run_id, "plan-revise")
    assert [row["status"] for row in attempts] == ["submitting", "completed"]
    assert [row["attempt_kind"] for row in attempts] == ["role", "technical_retry"]
    assert sum(r["metadata"]["podgen_stage"] == "plan-revise" for r in provider.requests) == 2
    assert sum(r["metadata"]["podgen_stage"] == "plan" for r in provider.requests) == 1


def test_live_access_is_rechecked_after_planning_count(own_dataset):
    repo, project, _, _, _ = own_dataset
    command = command_for(own_dataset)
    provider = OwnPlanProvider()

    def withdraw():
        with repo.database.transaction() as tx:
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                p=project,
                a=repo.actor_id,
            )

    provider.on_count = withdraw
    try:
        with pytest.raises(AccessDenied):
            execute(own_dataset, command, provider)
        assert not provider.requests
    finally:
        with repo.database.transaction() as tx:
            query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=repo.actor_id)


def test_a_prepared_review_cannot_be_rebound_to_a_changed_candidate(own_dataset):
    command = command_for(own_dataset)
    provider = OwnPlanProvider()

    def stop_before_review():
        if len(provider.requests) == 1:
            raise KeyboardInterrupt()

    provider.on_count = stop_before_review
    with pytest.raises(KeyboardInterrupt):
        execute(own_dataset, command, provider)
    journal = EvaluationJournal(own_dataset[0], command.project_id)
    original = journal.invocation(command.run_id, "plan")
    altered = journal.output(original)
    altered["output"]["plan"]["main_question"] += " SYNTHETIC substituted version."
    substitute = journal.record(altered, record_id=uuid4())
    journal.transition(original, "completed", output_ref=substitute)
    provider.on_count = None
    with pytest.raises(ContractError, match="planning_request_binding_mismatch"):
        execute(own_dataset, command, provider)
    assert len(provider.requests) == 1


def test_access_withdrawal_closes_all_planning_archives_but_unused_fixed_plan_does_not(own_dataset):
    from podgen.domain.contracts.common import BlobRef
    from podgen.runtime.evaluation.planning import clearance_ref

    repo, project, report, _, _ = own_dataset
    fixed_ref = RuntimeRef.model_validate(report["file_refs"]["author/gps-clock/plan.md"])
    fixed = repo.get_runtime(project, fixed_ref)
    from tests.authorization_helpers import withdraw_blob

    withdraw_blob(repo, project, BlobRef.model_validate(fixed["original_blob"]))
    command = command_for(own_dataset)
    provider = OwnPlanProvider(decisions=("accept", "accept"))
    execute(own_dataset, command, provider)
    journal = EvaluationJournal(repo, project)
    protected = [clearance_ref(command.run_id)]
    raw = []
    for stage in ("plan", "plan-review"):
        invocation = journal.invocation(command.run_id, stage)
        protected += [
            RuntimeRef(record_kind="tool_execution", record_id=invocation[key])
            for key in ("request_record_id", "output_record_id")
        ]
        for event in journal.events(invocation):
            ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
            saved = journal.read(ref)
            if saved.get("response_blob"):
                raw.append((ref, BlobRef.model_validate(saved["response_blob"])))
    for ref in protected:
        journal.read(ref)
    source = next(
        d
        for d in load_own_plan_materials(
            Repository(repo.database, repo.objects, repo.actor_id, purpose="generation"),
            project,
            command.author_ref,
            case_id="gps-clock",
        ).documents
        if d.path.endswith(".txt")
    )
    file = repo.get_runtime(project, source.ref)
    withdraw_blob(repo, project, BlobRef.model_validate(file["original_blob"]))
    for ref in protected:
        with pytest.raises(AccessDenied):
            journal.read(ref)
    for ref, blob in raw:
        with pytest.raises(AccessDenied):
            journal.read_blob(ref, blob)
    with pytest.raises(AccessDenied):
        execute(own_dataset, command, provider)
    assert len(provider.requests) == 5
    snow = RuntimeRef.model_validate(report["author_inputs"]["snow-pump"])
    assert load_own_plan_materials(
        Repository(repo.database, repo.objects, repo.actor_id, purpose="generation"),
        project,
        snow,
        case_id="snow-pump",
    ).documents
