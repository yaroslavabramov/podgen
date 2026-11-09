"""Real planning payloads share the writer's estimate; old pins keep their full requests."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_request import compile_role_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.author_reply import contract_pin
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import CATALOG_KEY, preview_references
from podgen.writing.planning_duration import KEY, VERSION, from_manifest
from podgen.writing.projection_versions import V8, V9

from tests.historical_preview import replay_preview as preview_request
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture

ROOT = Path(__file__).resolve().parents[2]
STAGES = ("plan", "plan-review", "plan-revise", "plan-review-revised")


def real_case(case, language=None):
    packet = EditorialInput.model_validate_json(
        (ROOT / "evals/editorial-inputs/current" / case / "shared.json").read_bytes()
    )
    if language:
        packet = packet.model_copy(
            update=dict(assignment=packet.assignment.model_copy(update=dict(output_language=language)))
        )
    if case == "snow-pump":
        plan = json.loads(
            (ROOT / "evals/reports/2026-11-07-snow-full-episode/accepted-plan.json").read_text()
        )
    else:
        plan = json.loads((ROOT / "evals/editorial-inputs/current/gps-clock/fixed-plan.json").read_text())[
            "plan"
        ]
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    _, _, _, profile, *_, review = fixture()
    review["findings"][0]["material_refs"] = [materials.material_catalog[0]]
    return materials, plan, profile, review


def stage_inputs(stage, plan, review):
    if stage == "draft":
        return dict(approved_plan=plan)
    return (
        {}
        if stage == "plan"
        else dict(candidate=plan, **(dict(plan_review=review) if stage == "plan-revise" else {}))
    )


@pytest.mark.parametrize(
    "case, language, calibrated",
    [("snow-pump", None, True), ("gps-clock", None, False), ("snow-pump", "en", False)],
)
def test_all_real_planning_previews_receive_exactly_the_writers_existing_duration_context(
    case, language, calibrated
):
    materials, plan, profile, review = real_case(case, language)
    writer = payload(preview_request(uuid4(), "draft", profile, materials, approved_plan=plan))
    expected = writer["duration_context"]
    assert ("estimated_word_target" in expected) is calibrated
    if calibrated:
        assert expected["estimated_word_target"] == 1650
    for stage in STAGES:
        current = payload(
            preview_request(uuid4(), stage, profile, materials, **stage_inputs(stage, plan, review))
        )
        assert current["duration_context"] == expected
        assert "requested" not in current["duration_context"]
        assert current["duration_context"]["current_text"] is None
        assert current["duration_context"]["audio_measured"] is False
        assert current["assignment"] == writer["assignment"]
        assert "documents" not in current and "source_documents" not in current
        if not calibrated:
            assert set(expected) == {"current_text", "audio_measured"}


@pytest.mark.parametrize("version", [V8, V9])
@pytest.mark.parametrize("stage", STAGES)
def test_saved_planning_requests_without_duration_pin_remain_exactly_the_previous_projection(version, stage):
    materials, plan, profile, review = real_case("snow-pump")
    refs = preview_references(materials, version)
    schema = response_schema(stage, "editorial-role-packets/1", projection_version=version)
    manifest = dict(
        role_projection_version=version, response_schemas={stage: schema}, **{CATALOG_KEY: refs.as_payload()}
    )
    if version == V9:
        manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
    saved = PreviewSnapshot(manifest, "Archived exact planning instructions.\r\n")
    run_id = uuid4()
    inputs = stage_inputs(stage, plan, review)
    expected = compile_role_request(
        run_id,
        stage,
        profile,
        materials,
        saved.instructions,
        schema,
        projection_version=version,
        reference_catalog=refs,
        **inputs,
    )
    replay = preview_request(run_id, stage, profile, materials, snapshot=saved, **inputs)
    assert replay == expected
    assert "duration_context" not in payload(replay)
    assert preview_request(run_id, stage, profile, materials, snapshot=saved, **inputs) == replay
    if version == V9:
        manifest[KEY] = VERSION
        enabled = preview_request(run_id, stage, profile, materials, snapshot=saved, **inputs)
        before, after = payload(replay), payload(enabled)
        estimate = after.pop("duration_context")
        assert after == before
        assert estimate["estimated_word_target"] == 1650
        assert enabled["instructions"] == replay["instructions"]
        assert enabled["text"] == replay["text"]


def test_duration_pin_rejects_unknown_or_historical_projection_contracts():
    assert from_manifest({}) is None
    assert from_manifest(dict(role_projection_version=V9, **{KEY: VERSION})) == VERSION
    for pin, version in [("unknown", V9), (VERSION, V8)]:
        with pytest.raises(ContractError, match="unsupported_planning_duration_contract"):
            from_manifest(dict(role_projection_version=version, **{KEY: pin}))
