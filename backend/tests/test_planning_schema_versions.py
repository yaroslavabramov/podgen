"""V6 changes planning descriptions only; historical schema text remains byte-reproducible."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.role_request import compile_reading_request, compile_role_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import ModelReferences
from podgen.writing.planning_descriptions import STORY_DESCRIPTIONS
from podgen.writing.projection_versions import V5, V6
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture

ROOT = Path(__file__).resolve().parents[2]


def test_every_v1_to_v5_schema_retains_its_pre_change_hash():
    before = json.loads((ROOT / "backend/tests/fixtures/role-schema-v1-v5.hashes.json").read_text())
    for version, stages in before.items():
        for stage, expected in stages.items():
            schema = response_schema(stage, "editorial-role-packets/1", projection_version=version)
            wire = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
            assert hashlib.sha256(wire).hexdigest() == expected, (version, stage)


def without_descriptions(value):
    if isinstance(value, dict):
        return {key: without_descriptions(child) for key, child in value.items() if key != "description"}
    if isinstance(value, list):
        return [without_descriptions(child) for child in value]
    return value


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_actual_current_plan_and_revision_wires_change_only_field_descriptions(case):
    packet = EditorialInput.model_validate_json(
        (ROOT / "evals/editorial-inputs/current" / case / "shared.json").read_bytes()
    )
    materials = EditorialMaterials(
        packet, RuntimeRef(record_kind="user_input", record_id=uuid5(NAMESPACE_URL, case)), ()
    )
    refs = ModelReferences.create(packet)
    if case == "gps-clock":
        plan = json.loads((ROOT / "evals/editorial-inputs/current/gps-clock/fixed-plan.json").read_text())[
            "plan"
        ]
    else:
        plan = refs.translate(
            json.loads((ROOT / "evals/reports/2026-11-07-snow-plan/03-planner-revision.json").read_text()),
            inbound=True,
        )
    _, _, _, profile, script, reader, fact, review = fixture()
    fact["findings"][0]["support_labels"] = [refs.card_ids[0]]
    review["findings"][0]["material_refs"] = [refs.card_ids[0]]
    script["utterances"] = [
        dict(speaker=s.label, text="Exact spoken words.") for s in packet.assignment.speakers
    ]
    from podgen.writing.prompts import load_instruction_set

    instructions = load_instruction_set(materials.format, include_planning=True)
    inputs = {
        "plan": {},
        "plan-revise": dict(candidate=plan, plan_review=review),
        "plan-review": dict(candidate=plan),
        "plan-review-revised": dict(candidate=plan),
        "draft": dict(approved_plan=plan),
        "review": dict(approved_plan=plan, draft=script),
        "revise": dict(approved_plan=plan, draft=script, feedback=dict(reader, reading_mode="informed")),
    }
    for stage, values in inputs.items():
        bodies = [
            compile_role_request(
                uuid5(NAMESPACE_URL, case),
                stage,
                profile,
                materials,
                instructions["plan-review" if stage == "plan-review-revised" else stage].body,
                response_schema(stage, "editorial-role-packets/1", projection_version=version),
                projection_version=version,
                **values,
            )
            for version in (V5, V6)
        ]
        old, new = bodies
        assert payload(new) == payload(old)
        assert new["input"] == old["input"]
        if stage in ("plan", "plan-revise"):
            assert new != old
            assert without_descriptions(new) == without_descriptions(old)
            definitions = new["text"]["format"]["schema"]["$defs"]
            for name, fields in STORY_DESCRIPTIONS.items():
                for field, description in fields.items():
                    assert definitions[name]["properties"][field]["description"] == description
        else:
            assert new == old
    from podgen.writing.prompts import load_reading_instructions

    for stage in ("reader-review", "fact-review", "revise-1"):
        values = dict(script=script)
        if stage == "revise-1":
            values.update(
                readings=dict(reader=reader, fact=fact),
                decision=dict(recommendation="revise", reason="required_changes_from_independent_readings"),
                approved_plan=plan,
            )
        bodies = [
            compile_reading_request(
                uuid5(NAMESPACE_URL, case),
                stage,
                profile,
                materials,
                load_reading_instructions("dual-revise" if stage == "revise-1" else stage).body,
                response_schema(stage, "editorial-role-packets/1", projection_version=version),
                projection_version=version,
                **deepcopy(values),
            )
            for version in (V5, V6)
        ]
        assert bodies[0] == bodies[1]
