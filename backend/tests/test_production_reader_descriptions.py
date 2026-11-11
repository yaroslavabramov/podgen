"""Production-only whole-episode field tasks retain every published reader shape."""

import json
from copy import deepcopy
from uuid import uuid4

from podgen.domain.contracts.common import ArtifactRef
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.behavior import resolve_behavior
from podgen.writing.reader_episode import FIELDS, FINDING_FIELDS
from podgen.writing.reader_schema import (
    COMMISSION_VERSION,
    COMPACT_VERSION,
    PRODUCTION_VERSION,
    V5,
    VERSION,
    feedback_model,
)

from tests.schema_cases import without_descriptions
from tests.source_excerpt_cases import data_and_profile, release


def test_production_reader_changes_only_field_descriptions_in_all_rounds():
    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        old = response_schema(stage, "editorial-role-packets/1", reader_schema_version=V5)
        new = response_schema(stage, "editorial-role-packets/1", reader_schema_version=PRODUCTION_VERSION)
        assert json.dumps(without_descriptions(new)) == json.dumps(without_descriptions(old))
        assert list(new["properties"]) == list(old["properties"])
        assert new["required"] == old["required"]
        for field, description in FIELDS.items():
            assert new["properties"][field]["description"] == description
        for field, description in FINDING_FIELDS.items():
            assert new["$defs"]["EditorialFinding"]["properties"][field]["description"] == description
        assert (
            "Lead with required repairs to the whole telling" in new["properties"]["findings"]["description"]
        )
        assert "First inspect the actual spoken wording" in old["properties"]["findings"]["description"]
        current = response_schema(stage, "editorial-role-packets/1", reader_schema_version=COMMISSION_VERSION)
        from podgen.writing.reader_commission import FINDING_FIELDS as commission_fields

        assert without_descriptions(current) == without_descriptions(new)
        assert all(
            current["$defs"]["EditorialFinding"]["properties"][k]["description"] == v
            for k, v in commission_fields.items()
        )
    assert feedback_model(COMMISSION_VERSION) is feedback_model(PRODUCTION_VERSION) is feedback_model(V5)


def test_other_profiles_and_other_roles_keep_their_previous_schemas():
    assert resolve_behavior("production/1").reader_schema == "listener-experience/14"
    assert resolve_behavior("existing/1").reader_schema == VERSION
    for name in (
        "narrative-rebuild/1",
        "narrative-rebuild/2",
        "editorial-candidate/1",
        "editorial-candidate/2",
    ):
        assert resolve_behavior(name).reader_schema == V5
    for stage in (
        "plan",
        "plan-review",
        "plan-revise",
        "draft",
        "revise-1",
        "fact-review",
        "fact-review-1",
        "fact-review-2",
    ):
        old = response_schema(stage, "editorial-role-packets/1", reader_schema_version=V5)
        new = response_schema(stage, "editorial-role-packets/1", reader_schema_version=PRODUCTION_VERSION)
        assert new == old


def test_new_release_pins_episode_descriptions_and_an_old_snapshot_replays_the_old_tasks(monkeypatch):
    materials, _, _, _, script, _, _, _, profile = data_and_profile()
    store, _, command, manifest = release(monkeypatch, materials, profile)
    assert manifest["reader_review_schema_version"] == "listener-experience/14"
    # These historical releases used cold reads in every round. Keep their
    # snapshot shape explicit now that production repeated reads use /2.
    cold = deepcopy(manifest)
    cold.pop("revision_verification_contract")
    cold.pop("reading_audit_contract")
    cold.pop("reading_audit_consistency_contract")
    cold.pop("reading_audit_memory_projection")
    cold["reader_review_schema_version"] = COMPACT_VERSION
    cold["fact_review_schema_version"] = "fact-revision/4"
    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        cold["response_schemas"][stage] = response_schema(
            stage, command.input_contract, reader_schema_version=COMPACT_VERSION
        )
    old = deepcopy(cold)
    old["reader_review_schema_version"] = V5
    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        old["response_schemas"][stage] = response_schema(
            stage, command.input_contract, reader_schema_version=V5
        )
        instructions = store.get(
            command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"]["reader-review"])
        ).payload["template"]
        new_body = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(cold, instructions),
            draft=script,
        )
        old_body = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(old, instructions),
            draft=script,
        )
        assert old_body["text"]["format"]["schema"] == old["response_schemas"][stage]
        new_body["text"]["format"]["schema"] = old_body["text"]["format"]["schema"]
        assert new_body == old_body
        assert (
            preview_request(
                command.run_id,
                stage,
                profile(stage),
                materials,
                snapshot=PreviewSnapshot(old, instructions),
                draft=script,
            )
            == old_body
        )
    from podgen.writing.revision.verification import bind_assignments

    assert preview_request(uuid4(), "reader-review", profile("reader-review"), materials, draft=script)[
        "text"
    ]["format"]["schema"] == bind_assignments(
        manifest["response_schemas"]["reader-review"], {"assignments": []}
    )
