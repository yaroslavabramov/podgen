"""Actual provider schemas and the real 83 preview retain source text, not private fact audit."""

import json
from pathlib import Path
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation import script_revision
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.reader_schema import V2
from podgen.writing.reader_schema import VERSION as READER_VERSION
from podgen.writing.revision.contracts import FACT_VERSION
from tests.revision_cases import actual_case
from tests.test_editorial_independence import payload
from tests.test_model_wire_schema import assert_reference_nodes
from tests.test_script_handoff import wire_hash
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
    assert reader["listener_understanding"] == reviews["reader"]["listener_answer"]
    assert not {"listener_understanding", "duration_note", "important_limits"} & set(fact)
    assert len(fact["findings"]) == len(reviews["fact"]["findings"])
    assert all(finding["evidence"] and finding["preserve"] for finding in fact["findings"])
    assert request["text"]["format"]["schema"]["title"] == "TargetedRevision"
    assert_reference_nodes(request["text"]["format"]["schema"])


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
