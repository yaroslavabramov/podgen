"""Actual provider schemas and the real 83 preview retain source text, not private fact audit."""

import json
from pathlib import Path
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation import script_revision
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.reader_schema import V2
from podgen.writing.reader_schema import VERSION as READER_VERSION
from podgen.writing.revision.contracts import FACT_VERSION

from tests.historical_preview import replay_preview as preview_request
from tests.revision_cases import actual_case
from tests.schema_cases import wire_hash
from tests.test_editorial_independence import payload
from tests.test_model_wire_schema import assert_reference_nodes
from tests.test_v5_role_wire import fixture


def test_pre_addressed_review_schemas_stay_byte_identical():
    expected = json.loads((Path(__file__).parent / "fixtures/pre-addressed-review-schemas.json").read_text())
    for stage in ("reader-review", "fact-review"):
        schema = response_schema(
            stage, "editorial-role-packets/1", reader_schema_version=V2 if stage == "reader-review" else None
        )
        assert wire_hash(schema) == expected[stage]


def test_real_83_normal_preview_uses_targeted_schema_and_keeps_the_full_script():
    _, _, script, reviews = actual_case()
    root = Path(__file__).resolve().parents[2]
    packet = EditorialInput.model_validate_json(
        (root / "evals/editorial-inputs/current/snow-pump/shared.json").read_bytes()
    )
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    request = preview_request(
        uuid4(),
        "revise-1",
        fixture()[3],
        materials,
        draft=script,
        reader_feedback=reviews["reader"],
        fact_feedback=reviews["fact"],
    )
    visible = payload(request)
    assert [row["text"] for row in visible["script"]["utterances"]] == [
        row["text"] for row in script["utterances"]
    ]
    assert visible["revision_operation"]["mode"] == "targeted"
    assert "base_script_hash" not in visible["revision_operation"]
    reader, fact = visible["revision_guidance"]
    assert "listener_understanding" not in reader and "craft_reference" not in visible
    assert reader["findings"] == reviews["reader"]["findings"]
    assert reader["positive_observations"] == reviews["reader"]["strengths"]
    assert fact["positive_observations"] == reviews["fact"]["strengths"]
    assert not {"listener_understanding", "duration_note", "important_limits"} & set(fact)
    assert len(fact["findings"]) == len(reviews["fact"]["findings"])
    assert all(finding["evidence"] and finding["preserve"] for finding in fact["findings"])
    schema = request["text"]["format"]["schema"]
    assert schema["properties"]["result"]["anyOf"][0]["title"] == "TargetedRevision"
    targets = visible["revision_operation"]["targets"]
    text_ids = [row["target_id"] for row in targets if "text" in row]
    boundaries = [row["target_id"] for row in targets if "before_utterance_index" in row]
    for operation in ("ReplaceText", "DeleteText"):
        assert schema["$defs"][operation]["properties"]["target_id"]["enum"] == text_ids
    assert schema["$defs"]["InsertUtterances"]["properties"]["target_id"]["enum"] == boundaries
    assert_reference_nodes(schema)


def test_every_new_provider_schema_has_valid_reference_nodes_and_explicit_required_fields():
    for stage in ("reader-review", "fact-review"):
        schema = response_schema(
            stage,
            "editorial-role-packets/1",
            reader_schema_version=READER_VERSION,
            fact_schema_version=FACT_VERSION,
        )
        assert_reference_nodes(schema)
        for node in (schema, *schema.get("$defs", {}).values()):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
    for schema in script_revision.schemas().values():
        assert_reference_nodes(schema)
        for node in (schema, *schema.get("$defs", {}).values()):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False


def test_compact_revision_pin_requires_its_addressed_review_workflow():
    import pytest
    from podgen.domain.errors import ContractError
    from podgen.writing.projection_versions import V8, V9
    from podgen.writing.revision.context import KEY, VERSION, from_manifest

    current = {
        "role_projection_version": V9,
        "script_revision_contract": "addressed-script-revision/1",
        KEY: VERSION,
    }
    assert from_manifest(current) == VERSION
    assert from_manifest({k: v for k, v in current.items() if k != KEY}) is None
    for invalid in (
        dict(current, role_projection_version=V8),
        {k: v for k, v in current.items() if k != "script_revision_contract"},
        dict(current, revision_context_contract="unknown"),
    ):
        with pytest.raises(ContractError, match="unsupported_revision_context_contract"):
            from_manifest(invalid)


def test_compact_rewrite_wire_changes_only_the_tutorial_and_listener_recap():
    from dataclasses import replace

    from podgen.writing.author_reply import contract_pin
    from podgen.writing.craft_reference import load_reference
    from podgen.writing.plan_selection import VERSION as SCENE_LINKS
    from podgen.writing.revision.context import KEY, VERSION

    from tests.revision_cases import scope
    from tests.test_plan_selection import snapshot

    materials, _, plan, profile, script, reader, fact, _ = fixture()
    for review in (reader, fact):
        review["revision_scope"] = scope(anchors=review["findings"][0]["anchors"])
    prior = snapshot(materials, profile, "revise-1", SCENE_LINKS)
    prior.manifest.update(
        script_revision_contract="addressed-script-revision/1",
        revision_response_schemas=script_revision.schemas(),
        craft_reference=load_reference(materials.output_language),
        output_language=materials.output_language,
    )
    prior.manifest["writer_response_contract"] = contract_pin(prior.manifest, materials.speaker_labels)
    current = replace(prior, manifest={**prior.manifest, KEY: VERSION})
    run_id = uuid4()
    old, new = [
        preview_request(
            run_id,
            "revise-1",
            profile,
            materials,
            approved_plan=plan,
            draft=script,
            reader_feedback=reader,
            fact_feedback=fact,
            snapshot=pin,
        )
        for pin in (prior, current)
    ]
    old_payload = payload(old)
    assert old_payload.pop("craft_reference") == prior.manifest["craft_reference"]["text"]
    assert old_payload["revision_guidance"][0].pop("listener_understanding") == reader["listener_answer"]
    assert old_payload == payload(new)
    assert {k: v for k, v in old.items() if k != "input"} == {k: v for k, v in new.items() if k != "input"}
