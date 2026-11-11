"""Pinned continuation admission and exact writer preview without providers or a database."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import reading_stage
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.editorial_continuation import source
from podgen.runtime.evaluation.editorial_continuation.manifest import assemble, verify_assembly
from podgen.runtime.evaluation.editorial_continuation.preview import attach
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.workflow import stages_for

from tests.source_excerpt_cases import Captured, CaptureGateway, release
from tests.test_required_revision_assignment import case


def fixture(monkeypatch, consumed=2):
    materials, _, script, readings, profile = case("local")
    store, journal, root, parent = release(monkeypatch, materials, profile)
    root = WritingRunCommand.model_validate(
        dict(
            root.model_dump(mode="json"),
            plan_mode="fixed-plan",
            planner_profile_ref=None,
            plan_reviewer_profile_ref=None,
            max_script_revisions=consumed,
        )
    )
    parent.update(command=command_payload(root), plan_mode="fixed-plan")
    script_ref = journal.save(dict(output=script))
    refs = {
        kind: journal.save(dict(output=review, script_ref=script_ref, script_hash=digest(script)))
        for kind, review in readings.items()
    }
    outcome = dict(
        status="revision_limit_exhausted",
        script_ref=script_ref,
        script_hash=digest(script),
        reading_refs=refs,
        token_usage_or_reservation=dict(input=1000, output=500),
    )
    manifests, outcomes = {root.run_id: parent}, {root.run_id: outcome}
    journal.manifest = lambda identity: manifests[identity]
    journal.run = lambda identity: dict(status="needs_attention") if identity in manifests else None
    monkeypatch.setattr(source, "read_outcome", lambda owner, identity: outcomes[identity])
    monkeypatch.setattr(
        source,
        "effective_ceiling",
        lambda owner, identity: manifests[identity]["command"]["limits"]["max_total_input_tokens"],
    )
    child = source.build_command(journal, root.run_id, additional_input=524288)
    binding, _, _ = source.source_binding(journal, child, materials)
    frozen = assemble(child, parent, binding)
    manifests[child.run_id] = frozen
    return SimpleNamespace(**locals())


def test_deterministic_admission_preserves_pins_and_inherited_cost(monkeypatch):
    c = fixture(monkeypatch)
    assert c.child.run_id == source.continuation_id(c.root.run_id)
    assert source.build_command(c.journal, c.root.run_id, additional_input=524288) == c.child
    assert c.frozen[source.KEY]["inherited_usage"] == dict(input=1000, output=500)
    assert c.child.limits.max_total_input_tokens == c.root.limits.max_total_input_tokens + 524288
    assert c.child.limits.max_total_output_tokens == c.root.limits.max_total_output_tokens
    assert c.child.max_script_revisions == 1 and source.offset(c.frozen) == 2
    assert stages_for(c.child) == ("revise-1", "reader-review-1", "fact-review-1")
    for field in ("prompt_refs", "role_instructions_versions", "response_schemas"):
        assert c.frozen[field]["revise-1"] == c.parent[field]["revise-2"]
        assert c.frozen[field]["reader-review-1"] == c.parent[field]["reader-review-2"]
    with pytest.raises(ContractError, match="allowance_changed"):
        source.build_command(c.journal, c.root.run_id, additional_input=524289)
    with pytest.raises(ContractError, match="run_id_mismatch"):
        source.build_command(c.journal, c.root.run_id, additional_input=524288, run_id=uuid4())


@pytest.mark.parametrize(
    "field",
    [
        "prompt_refs",
        "response_schemas",
        "role_instructions_versions",
        "materials_hash",
        "prompt_cache_layout",
        "command",
    ],
)
def test_changed_inherited_resource_cannot_pass_with_unchanged_binding(monkeypatch, field):
    c = fixture(monkeypatch)
    changed = deepcopy(c.frozen)
    changed[field] = {"changed": True} if isinstance(changed[field], dict) else "changed"
    with pytest.raises(ContractError, match="resources_changed"):
        verify_assembly(c.child, c.parent, c.binding, changed)


@pytest.mark.parametrize("field", ["script_ref", "reading_refs", "script_hash"])
def test_seed_substitution_is_rejected(monkeypatch, field):
    c = fixture(monkeypatch)
    changed = deepcopy(c.frozen)
    changed[source.KEY][field] = {"changed": True} if isinstance(c.binding[field], dict) else "changed"
    with pytest.raises(ContractError, match="seed_changed"):
        source.seed(c.journal, changed)


def test_material_change_is_rejected_and_global_three_revisions_cannot_reset(monkeypatch):
    c = fixture(monkeypatch, consumed=1)
    other = replace(
        c.materials,
        packet=c.materials.packet.model_copy(
            update=dict(
                assignment=c.materials.packet.assignment.model_copy(update=dict(audience="Changed audience"))
            )
        ),
    )
    with pytest.raises(ContractError, match="materials_changed"):
        source.source_binding(c.journal, c.child, other)
    c.outcomes[c.child.run_id] = dict(c.outcome, token_usage_or_reservation=dict(input=1300, output=650))
    next_child = source.build_command(c.journal, c.child.run_id, additional_input=0)
    binding, _, _ = source.source_binding(c.journal, next_child, c.materials)
    assert binding["revisions_completed_before"] == 2
    assert binding["inherited_usage"] == dict(input=1300, output=650)
    next_manifest = assemble(next_child, c.frozen, binding)
    c.manifests[next_child.run_id] = next_manifest
    c.outcomes[next_child.run_id] = c.outcome
    with pytest.raises(ContractError, match="revision_limit_exhausted"):
        source.build_command(c.journal, next_child.run_id, additional_input=0)


def test_changed_parent_output_refs_and_non_exhausted_outcome_are_rejected(monkeypatch):
    c = fixture(monkeypatch)
    c.outcome["output_refs"] = {"revise-2": {"different": "output"}}
    with pytest.raises(ContractError, match="seed_changed"):
        source.seed(c.journal, c.frozen)
    c.outcome["status"] = "keep"
    with pytest.raises(ContractError, match="requires_exhausted_run"):
        source.build_command(c.journal, c.root.run_id, additional_input=0)


def test_no_historical_revision_resources_means_explicit_refusal(monkeypatch):
    with pytest.raises(ContractError, match="revision_resources_missing"):
        fixture(monkeypatch, consumed=0)


def test_first_preview_and_runtime_use_global_version_and_exact_parent_tasks(monkeypatch):
    c = fixture(monkeypatch)
    instructions = c.store.get(
        c.child.project_id,
        ArtifactRef.model_validate(c.frozen["prompt_refs"]["revise-1"]),
    ).payload["template"]
    snapshot = attach(PreviewSnapshot(c.frozen, instructions), c.journal, c.child.run_id, "revise-1")
    preview = preview_request(
        c.child.run_id, "revise-1", c.profile("revise-1"), c.materials, snapshot=snapshot
    )
    visible = request_payload(preview)
    assert visible["revision_operation"]["script_version"] == 2
    with pytest.raises(Captured):
        reading_stage.execute_reading_stage(
            "revise-1",
            journal=c.journal,
            gateway=CaptureGateway(),
            generation=c.store,
            command=c.child,
            manifest=c.frozen,
            materials=c.materials,
            totals=dict(input=1000, output=500),
            guard=lambda: None,
            script=c.script,
            script_ref=c.script_ref,
            readings=c.readings,
            reading_refs=c.refs,
            decision=combine(c.readings),
        )
    assert c.journal.request(c.journal.invocation(c.child.run_id, "revise-1")) == preview
    with pytest.raises(ContractError, match="preview_editorial_continuation_binding_mismatch"):
        preview_request(
            c.child.run_id,
            "revise-1",
            c.profile("revise-1"),
            c.materials,
            snapshot=snapshot,
            reader_feedback={"different": "review"},
        )
