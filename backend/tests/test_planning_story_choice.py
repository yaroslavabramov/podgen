"""Two short editorial concepts stay in planning; only the selected developed story reaches writing."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.planning_export import render_plan
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.author_reply import contract_pin
from podgen.writing.editorial_contract import FixedEditorialInput
from podgen.writing.model_references import CATALOG_KEY, preview_references
from podgen.writing.planning_choice import KEY, VERSION, PlanningChoiceOutput, from_manifest
from podgen.writing.planning_versions import planning_model, planning_snapshot
from podgen.writing.projection_versions import V8, V9
from tests.planning_choice_cases import selection
from tests.test_editorial_independence import payload
from tests.test_planning_v9 import selected
from tests.test_v5_role_wire import fixture


def chosen(source=None):
    return dict(selected(source), story_selection=selection())


def test_new_choice_schema_is_explicit_and_old_v9_schema_keeps_its_exact_shape():
    old = response_schema("plan", "editorial-role-packets/1", projection_version=V9)
    for stage in ("plan", "plan-revise"):
        wire = response_schema(
            stage, "editorial-role-packets/1", projection_version=V9, planning_output_contract=VERSION
        )
        assert list(wire["properties"])[0] == "story_selection"
        assert "story_selection" not in old["properties"]
        assert "story_selection" in wire["required"]
        assert set(wire["$defs"]["StorySelection"]["properties"]) == {
            "first",
            "second",
            "difference",
            "selected",
            "why_selected",
        }
        for node in (wire, *wire["$defs"].values()):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
        assert wire["$defs"]["PlanProposalV9"] == old["$defs"]["PlanProposalV9"]
    assert planning_model(V9, planning_output_contract=VERSION) is PlanningChoiceOutput
    assert planning_model(V9) is not PlanningChoiceOutput
    assert from_manifest({"role_projection_version": V9}) is None
    for pin, version in [("unknown", V9), (VERSION, V8)]:
        with pytest.raises(ContractError, match="unsupported_planning_output_contract"):
            from_manifest({"role_projection_version": version, KEY: pin})


def test_pre_choice_v9_planning_wires_remain_byte_reproducible():
    hashes = json.loads((Path(__file__).parent / "fixtures/v9-planning-schema.hashes.json").read_text())
    for stage, expected in hashes.items():
        for actual in (stage, "plan-revise" if stage == "plan" else "plan-review-revised"):
            schema = response_schema(actual, "editorial-role-packets/1", projection_version=V9)
            wire = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
            assert hashlib.sha256(wire).hexdigest() == expected


def test_complete_editorial_result_roundtrips_and_invalid_selection_cannot_silently_downgrade():
    plan = chosen()
    plan["story_selection"]["difference"] = "Complete editorial distinction.\n" * 10000
    assert planning_snapshot(plan).model_dump(mode="json") == plan
    fixed = dict(
        contract="fixed-editorial-input/2",
        assignment_key="test",
        shared_input_digest="0" * 64,
        document=dict(source_path="plan.json", sha256="1" * 64),
        plan=plan,
    )
    assert FixedEditorialInput.model_validate(fixed).model_dump(mode="json") == fixed
    with pytest.raises(ValidationError):
        PlanningChoiceOutput.model_validate(selected())
    for broken in (
        dict(plan, story_selection=None),
        dict(plan, story_selection=dict(plan["story_selection"], selected="third")),
    ):
        with pytest.raises(ValidationError):
            planning_snapshot(broken)
    blocked = dict(
        status="needs_research",
        plan=None,
        story_selection=None,
        research_requests=[
            dict(
                question="Missing material?",
                why_needed="The story needs it.",
                affected_act_ids=[],
                material_refs=[],
                blocking=True,
            )
        ],
    )
    assert planning_snapshot(blocked).model_dump(mode="json") == blocked


@pytest.mark.parametrize("stage", ["draft", "revise-1"])
def test_author_requests_are_identical_with_or_without_the_editorial_comparison(stage):
    materials, _, candidate, profile, script, reader, fact, _ = fixture()
    plan = chosen(candidate)
    old = deepcopy(plan)
    old.pop("story_selection")
    inputs = dict(approved_plan=plan)
    if stage != "draft":
        inputs.update(draft=script, reader_feedback=reader, fact_feedback=fact)
    run_id = uuid4()
    request = preview_request(run_id, stage, profile, materials, **inputs)
    inputs["approved_plan"] = old
    assert request == preview_request(run_id, stage, profile, materials, **inputs)
    assert "story_selection" not in json.dumps(payload(request))
    assert plan["story_selection"]["second"] not in json.dumps(request)


def test_review_receives_the_complete_comparison_and_old_preview_retains_old_schema_and_instructions():
    materials, _, candidate, profile, *_ = fixture()
    plan = chosen(candidate)
    review = preview_request(uuid4(), "plan-review", profile, materials, candidate=plan)
    assert payload(review)["candidate_plan"]["story_selection"] == plan["story_selection"]
    refs = preview_references(materials, V9)
    schema = response_schema("plan", "editorial-role-packets/1", projection_version=V9)
    manifest = dict(
        role_projection_version=V9, response_schemas={"plan": schema}, **{CATALOG_KEY: refs.as_payload()}
    )
    manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
    saved = PreviewSnapshot(manifest, "Exact archived planner instructions.\r\n")
    run_id = uuid4()
    before = preview_request(run_id, "plan", profile, materials, snapshot=saved)
    assert before["instructions"] == saved.instructions
    assert before["text"]["format"]["schema"]["properties"].keys() == schema["properties"].keys()
    assert preview_request(run_id, "plan", profile, materials, snapshot=saved) == before
    assert "story_selection" not in before["text"]["format"]["schema"]["properties"]


@pytest.mark.parametrize("language, heading", [("ru", "Выбор рассказа"), ("en", "Story selection")])
def test_readable_export_preserves_both_concepts_distinction_and_choice(language, heading):
    plan = chosen()
    text = render_plan(dict(output=plan), None, True, language)
    assert heading in text
    for field in ("first", "second", "difference", "why_selected"):
        assert plan["story_selection"][field] in text
    assert text.index(heading) < text.index(plan["plan"]["route_summary"])
    assert "story_selection" not in text and "why_selected" not in text
