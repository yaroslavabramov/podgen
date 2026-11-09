"""A released plan-editor variant changes only its instructions and pinned preserve description."""

import hashlib
import json
from copy import copy, deepcopy
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

import pytest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.author_reply import contract_pin
from podgen.writing.behavior import DEFAULT, NAMES, BehaviorProfile, resolve_behavior
from podgen.writing.model_references import CATALOG_KEY, preview_references
from podgen.writing.plan_review_descriptions import (
    PRESERVE_DESCRIPTION,
    PRESERVE_KEY,
    PRESERVE_VERSION,
    schema_version_from_manifest,
)
from podgen.writing.projection_versions import V8, V9

from tests.test_v5_role_wire import fixture


def test_narrative_rebuild_2_changes_only_the_plan_editor_and_preserves_frozen_request_validation(
    monkeypatch,
):
    assert "narrative-rebuild/2" in NAMES
    assert resolve_behavior().name == DEFAULT == "production/1"
    for passno in (1, 2, 3):
        old, new = (resolve_behavior(name, passno) for name in ("narrative-rebuild/1", "narrative-rebuild/2"))
        for field in fields(BehaviorProfile):
            if field.name not in ("name", "resources"):
                assert getattr(old, field.name) == getattr(new, field.name)
        assert old.verification == new.verification
        assert old.resources["readings"] == new.resources["readings"]
        assert old.resources["repairs"] == new.resources["repairs"]
        assert old.craft("ru") == new.craft("ru")
        assert old.plan_review_schema is None and new.plan_review_schema == PRESERVE_VERSION
        for fmt in ("solo", "dialogue"):
            assert {
                role
                for role in old.resources["formats"][fmt]
                if old.instructions(role, fmt) != new.instructions(role, fmt)
            } == {"plan-review"}
    for name in ("existing/1", "narrative-rebuild/1", "editorial-candidate/1", "editorial-candidate/2"):
        assert resolve_behavior(name).plan_review_schema is None
    previous_hashes = json.loads(
        (Path(__file__).parent / "fixtures/v9-planning-schema.hashes.json").read_text()
    )
    wire = response_schema("plan-review", "editorial-role-packets/1", projection_version=V9)
    assert (
        hashlib.sha256(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        == previous_hashes["plan-review"]
    )

    materials, _, plan, profile, script, reader, fact, review = fixture()
    run_id = uuid4()
    requests = {}
    values = {
        "plan": {},
        "plan-review": dict(candidate=plan),
        "plan-review-revised": dict(candidate=plan),
        "plan-revise": dict(candidate=plan, plan_review=review),
        "draft": dict(approved_plan=plan),
        "reader-review": dict(draft=script),
        "fact-review": dict(draft=script),
        "revise-1": dict(draft=script, approved_plan=plan, reader_feedback=reader, fact_feedback=fact),
    }
    for stage, inputs in values.items():
        configured = copy(profile)
        author = stage == "draft" or stage.startswith("revise-")
        configured.provider = "anthropic" if author else "openai"
        configured.api_family = "messages" if author else "responses"
        configured.adapter_version = "anthropic-messages-v1" if author else "openai-responses-v1"
        configured.model_revision_or_alias = (
            "claude-opus-5-5"
            if author
            else "gpt-6.1-sol"
            if stage in ("plan", "plan-revise")
            else "gpt-6.1-terra"
        )
        configured.context_limit, configured.output_limit = 200000, 24576
        configured.generation_parameters = dict(
            reasoning=dict(effort="medium"), text=dict(verbosity="high"), store=False, background=False
        )
        old = preview_request(
            run_id, stage, configured, materials, behavior_profile="narrative-rebuild/1", **inputs
        )
        new = preview_request(
            run_id, stage, configured, materials, behavior_profile="narrative-rebuild/2", **inputs
        )
        expected = deepcopy(old)
        if stage.startswith("plan-review"):
            assert old["instructions"] != new["instructions"]
            expected["instructions"] = new["instructions"]
            expected["text"]["format"]["schema"]["$defs"]["PlanReviewFinding"]["properties"]["preserve"][
                "description"
            ] = PRESERVE_DESCRIPTION
        assert new == expected
        requests[stage] = (old, new, configured, inputs)

    # The same schema-description pin is used by creation and resume schema recomputation.
    for stage in ("plan-review", "plan-review-revised"):
        for version in (None, PRESERVE_VERSION):
            manifest = dict(role_projection_version=V9)
            if version:
                manifest[PRESERVE_KEY] = version
            schema = response_schema(
                stage,
                "editorial-role-packets/1",
                plan_review_schema_version=schema_version_from_manifest(manifest),
            )
            assert schema["$defs"]["PlanReviewFinding"]["properties"]["preserve"]["description"] == (
                PRESERVE_DESCRIPTION
                if version
                else wire["$defs"]["PlanReviewFinding"]["properties"]["preserve"]["description"]
            )
    for marker, projection in [("unknown", V9), (PRESERVE_VERSION, V8)]:
        with pytest.raises(ContractError, match="unsupported_plan_review_schema_version"):
            schema_version_from_manifest(dict(role_projection_version=projection, **{PRESERVE_KEY: marker}))

    import podgen.runtime.evaluation.preview as preview_module

    def forbidden(*args, **kwargs):
        raise AssertionError("A saved preview must not resolve current behavior")

    monkeypatch.setattr(preview_module, "resolve_behavior", forbidden)
    refs = preview_references(materials, V9)
    for name, index in [("narrative-rebuild/1", 0), ("narrative-rebuild/2", 1)]:
        request = requests["plan-review"][index]
        manifest = dict(
            role_projection_version=V9,
            behavior_profile=name,
            command=dict(behavior_profile=name),
            response_schemas={"plan-review": request["text"]["format"]["schema"]},
            **{CATALOG_KEY: refs.as_payload()},
        )
        if index:
            manifest[PRESERVE_KEY] = PRESERVE_VERSION
        manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
        snapshot = PreviewSnapshot(manifest, request["instructions"])
        assert (
            preview_request(
                run_id,
                "plan-review",
                requests["plan-review"][2],
                materials,
                snapshot=snapshot,
                candidate=plan,
            )
            == request
        )

    # CLI run uses NAMES, preview uses the same resolver, resume parses the archived command.
    from podgen.domain.contracts.common import ArtifactRef, RuntimeRef

    ref = ArtifactRef(kind="ModelProfile", artifact_id=uuid4(), revision_id=uuid4())
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=uuid4(),
        actor_id=uuid4(),
        case_id="saved",
        author_ref=RuntimeRef(record_kind="user_input", record_id=uuid4()),
        writer_profile_ref=ref,
        reviewer_profile_ref=ref,
        behavior_profile="narrative-rebuild/2",
    )
    assert command_payload(WritingRunCommand.model_validate(command_payload(command))) == command_payload(
        command
    )
    rereading = command.model_copy(
        update=dict(
            editorial_pass=2, initial_script_ref=RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        )
    )
    assert (
        WritingRunCommand.model_validate(command_payload(rereading)).behavior_profile == "narrative-rebuild/2"
    )
