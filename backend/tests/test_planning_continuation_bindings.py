"""Source identity and previews cannot substitute a different plan, review or brief."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.planning_continuation.cli import prepare
from podgen.runtime.evaluation.planning_continuation.source import build_command, last_pair
from podgen.runtime.evaluation.preview_snapshot import load_preview_snapshot
from podgen.runtime.evaluation.profiles import load_profile
from podgen.runtime.evaluation.service import WritingEvaluation

from tests.historical_preview import replay_preview as preview_request
from tests.planning_continuation_cases import ContinuationProvider, failed_source
from tests.test_own_plan import own_dataset as own_dataset


def test_continuation_rejects_changed_command_before_inference(own_dataset, tmp_path, monkeypatch):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    child = build_command(journal, source.run_id, limit=2)
    wrong = child.model_copy(update={"max_script_revisions": 0})
    provider = ContinuationProvider("accept")
    with pytest.raises(ContractError, match="planning_continuation_command_binding_mismatch"):
        WritingEvaluation(own_dataset[0], lambda _: provider).run(wrong)
    assert not provider.requests


def test_latest_candidate_cannot_use_the_earlier_review(own_dataset, tmp_path, monkeypatch):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    original = journal.invocation
    monkeypatch.setattr(
        journal,
        "invocation",
        lambda run, stage: None if stage == "plan-review-revised" else original(run, stage),
    )
    with pytest.raises(ContractError, match="planning_continuation_review_missing"):
        last_pair(journal, source.run_id)


def test_pinned_preview_binds_both_seed_outputs_and_waits_for_the_new_candidate(
    own_dataset, tmp_path, monkeypatch
):
    source, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    child = build_command(journal, source.run_id, limit=2)
    folder = tmp_path / "child"
    prepare(own_dataset[0], child, folder)
    generation = generation_repo(own_dataset[0])
    materials = load_editorial_materials(
        generation, source.project_id, source.author_ref, case_id=source.case_id
    )
    snapshot = load_preview_snapshot(journal, generation, child.run_id, "plan-revise", materials)
    profile = load_profile(generation, source.project_id, child.planner_profile_ref, "plan-revise")
    candidate = json.loads((folder / "planning-seed-plan.json").read_text())
    review = json.loads((folder / "planning-seed-review.json").read_text())
    for name in ("candidate", "plan_review"):
        inputs = dict(candidate=deepcopy(candidate), plan_review=deepcopy(review))
        if name == "candidate":
            inputs[name]["plan"]["route_summary"] += "SYNTHETIC different candidate."
        else:
            inputs[name]["decision_reason"] += "SYNTHETIC different review."
        with pytest.raises(ContractError, match="preview_continuation_binding_mismatch"):
            preview_request(child.run_id, "plan-revise", profile, materials, snapshot=snapshot, **inputs)
    with pytest.raises(ContractError, match="preview_continuation_revision_missing"):
        load_preview_snapshot(journal, generation, child.run_id, "plan-review-revised", materials)


def test_a_historical_manifest_without_command_input_contract_cannot_be_misread_as_modern():
    profile = ArtifactRef(artifact_id=uuid4(), revision_id=uuid4(), kind="ModelProfile")
    command = WritingRunCommand(
        behavior_profile="existing/1",
        run_id=uuid4(),
        project_id=uuid4(),
        actor_id=uuid4(),
        case_id="legacy",
        author_ref=RuntimeRef(record_kind="user_input", record_id=uuid4()),
        writer_profile_ref=profile,
        reviewer_profile_ref=profile,
        planner_profile_ref=profile,
        plan_reviewer_profile_ref=profile,
        plan_mode="own-plan",
    )
    data = command_payload(command)
    data.pop("input_contract")
    journal = SimpleNamespace(
        run=lambda _: dict(failure_code="planning_revision_limit_exhausted"),
        manifest=lambda _: dict(input_contract="legacy-author-materials/1", command=data),
    )
    with pytest.raises(ContractError, match="planning_continuation_requires_editorial_own_plan"):
        build_command(journal, command.run_id, limit=2)
