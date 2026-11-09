"""Export both supported fixed-plan sources through the normal exporter, without model calls."""

import json
from copy import deepcopy
from uuid import uuid4

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.planning import clearance_ref
from podgen.runtime.evaluation.profiles import create_profile

from tests.test_editorial_inputs import editorial as editorial


def test_fixed_plan_export_preserves_imported_bytes_and_verified_journal_origin(repo, editorial, tmp_path):
    project, data, _, shared, imported, _ = editorial
    journal = EvaluationJournal(repo, project)
    profile = create_profile(
        repo, project, model="synthetic-export-only", context_limit=200000, output_limit=24576
    )
    original_shared = repo.get_runtime(project, shared)
    original_fixed = repo.get_runtime(project, imported)
    shared_bytes = repo.read_runtime_blob(
        project, shared, BlobRef.model_validate(original_shared["original_input_blob"])
    )
    fixed_bytes = repo.read_runtime_blob(
        project, imported, BlobRef.model_validate(original_fixed["original_input_blob"])
    )
    command = WritingRunCommand(
        behavior_profile="existing/1",
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        fixed_plan_input_ref=imported,
        writer_profile_ref=profile.ref,
        reviewer_profile_ref=profile.ref,
        review_mode="informed-v1",
    )

    def manifest(cmd):
        return dict(
            command=command_payload(cmd),
            author_ref=shared.model_dump(mode="json"),
            input_contract=cmd.input_contract,
            plan_mode=cmd.plan_mode,
            stages=[],
            output_language="ru",
            materials_blob=journal.blob(b'{"frozen_input":true}').model_dump(mode="json"),
        )

    imported_manifest = manifest(command)
    journal.create_run(command, imported_manifest)
    first = tmp_path / "imported"
    assert export_run(repo, project, command.run_id, first)["status"] == "prepared"
    assert (first / "editorial-input-original.json").read_bytes() == shared_bytes
    assert (first / "fixed-editorial-input-original.json").read_bytes() == fixed_bytes
    assert not (first / "fixed-plan-origin.json").exists()

    source = command.model_copy(update=dict(run_id=uuid4(), plan_mode="own-plan", fixed_plan_input_ref=None))
    source_manifest = manifest(source)
    journal.create_run(source, source_manifest)
    plan = deepcopy(original_fixed["content"]["plan"])
    plan["plan"]["route_summary"] = "Complete retained account.\n" * 3000
    candidate = dict(
        contract="writing-evaluation-output-v1", run_id=str(source.run_id), stage="plan", output=plan
    )
    candidate_ref = journal.record(candidate)
    review = dict(
        contract="writing-evaluation-output-v1",
        run_id=str(source.run_id),
        stage="plan-review",
        candidate_plan_ref=candidate_ref.model_dump(mode="json"),
        candidate_plan_hash=digest(plan),
        output=dict(decision="accept", research_requests=[]),
    )
    review_ref = journal.record(review)
    proof = dict(
        contract="writing-planning-clearance-v1",
        run_id=str(source.run_id),
        candidate_plan_ref=candidate_ref.model_dump(mode="json"),
        candidate_plan_hash=digest(plan),
        plan_review_ref=review_ref.model_dump(mode="json"),
        decision="accept",
    )
    clearance = journal.record(proof, record_id=clearance_ref(source.run_id).record_id)
    reused = command.model_copy(update=dict(run_id=uuid4(), fixed_plan_input_ref=clearance))
    reused_manifest = manifest(reused)
    journal.create_run(reused, reused_manifest)
    second = tmp_path / "approved"
    result = export_run(repo, project, reused.run_id, second)
    assert result["status"] == "prepared" and result["invocations"] == {}
    assert (second / "editorial-input-original.json").read_bytes() == shared_bytes
    assert not (second / "fixed-editorial-input-original.json").exists()

    def read(name):
        return json.loads((second / name).read_bytes())

    origin = read("fixed-plan-origin.json")
    approved = read(origin["files"]["plan"])
    assert approved["plan"] == plan and approved["candidate_hash"] == digest(plan)
    assert origin["clearance_ref"] == clearance.model_dump(mode="json")
    assert origin["candidate_ref"] == candidate_ref.model_dump(mode="json")
    assert origin["review_ref"] == review_ref.model_dump(mode="json")
    assert origin["shared_input_ref"] == shared.model_dump(mode="json")
    assert read(origin["files"]["clearance"]) == proof
    assert read(origin["files"]["candidate"]) == candidate
    assert read(origin["files"]["review"]) == review
    assert read(origin["files"]["manifest"]) == source_manifest
    before = {
        str(file.relative_to(second)): file.read_bytes() for file in second.rglob("*") if file.is_file()
    }
    assert export_run(repo, project, reused.run_id, second) == result
    assert {
        str(file.relative_to(second)): file.read_bytes() for file in second.rglob("*") if file.is_file()
    } == before
    assert journal.manifest(source.run_id) == source_manifest
    assert journal.manifest(reused.run_id) == reused_manifest
