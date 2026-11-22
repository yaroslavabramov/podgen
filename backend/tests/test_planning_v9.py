"""V9 ending selection, lossless historical snapshots and immutable older planning wires."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from podgen.domain.canonical import digest
from podgen.runtime.evaluation import script_revision
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.plan_targets import (
    PlanTargetChoice,
    PlanTargetChoices,
    PlanTargetRepair,
    allowed_review_targets,
)
from podgen.writing.planning_roles import PlanReviewOutput
from podgen.writing.planning_v9 import PlanningOutputV9
from podgen.writing.planning_versions import planning_model, planning_snapshot
from podgen.writing.projection_versions import V8, V9
from podgen.writing.reading_views import reading_payload
from podgen.writing.story_views import editorial_frame, narrative_decisions
from pydantic import ValidationError

from tests.revision_cases import composition
from tests.schema_cases import wire_hash
from tests.test_model_wire_schema import assert_reference_nodes
from tests.test_v5_role_wire import fixture


def selected(source=None):
    plan = deepcopy(source if source is not None else fixture()[2])
    plan["plan"]["working_answer"] = "MEANING_ACROSS_THE_EPISODE"
    plan["plan"]["ending"] = dict(
        last_moment="INITIAL_LAST_MOMENT",
        why_end_here="A particular stopping point, without another recap.",
        promises_to_close=[],
    )
    return plan


def test_current_planner_wire_has_a_distinct_last_moment_and_the_old_wire_is_frozen():
    expected = json.loads((Path(__file__).parent / "fixtures/v8-planning-schema.hashes.json").read_text())
    for stage, frozen in expected.items():
        assert wire_hash(response_schema(stage, "editorial-role-packets/1", projection_version=V8)) == frozen
    for stage in ("plan", "plan-revise"):
        schema = response_schema(stage, "editorial-role-packets/1", projection_version=V9)
        assert_reference_nodes(schema)
        ending = schema["$defs"]["PlanningFinalMoment"]
        assert set(ending["properties"]) == {"last_moment", "why_end_here", "promises_to_close"}
        for node in [schema, *schema["$defs"].values()]:
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
        assert planning_model(V9) is PlanningOutputV9
        assert planning_model(V9, legacy=True) is planning_model(V8)


def test_both_plan_shapes_roundtrip_without_slicing_or_filling_legacy_fields():
    old, new = fixture()[2], selected()
    new["plan"]["ending"]["last_moment"] = "  Full ending detail.\n" * 10000
    for value in (old, new):
        parsed = planning_snapshot(value)
        assert parsed.model_dump(mode="json") == value
        assert planning_snapshot(json.loads(parsed.model_dump_json())).model_dump(mode="json") == value
    incomplete = selected()
    del incomplete["plan"]["ending"]["why_end_here"]
    with pytest.raises(ValidationError):
        planning_snapshot(incomplete)
    ambiguous = selected()
    ambiguous["plan"]["ending"]["answer_to_deliver"] = "Legacy meaning"
    with pytest.raises(ValidationError):
        planning_snapshot(ambiguous)


def test_final_moment_is_addressable_and_selection_repair_preserves_the_judgment():
    data = fixture()
    candidate = planning_snapshot(selected(data[2]))
    review = deepcopy(data[-1])
    review["findings"][0]["targets"] = ["/plan/ending/nonexistent"]
    parsed = PlanReviewOutput.model_validate(review)
    repair = PlanTargetRepair.build(candidate, parsed, data[0].material_catalog)
    assert "/plan/ending/last_moment" in allowed_review_targets(candidate.model_dump(mode="json"))
    fixed = repair.apply(
        PlanTargetChoices(
            choices=[
                PlanTargetChoice(
                    target_id=repair.invalid[0].target_id, selected_path="/plan/ending/last_moment"
                )
            ]
        )
    )
    assert fixed.findings[0].observation == parsed.findings[0].observation
    assert fixed.findings[0].targets == ["/plan/ending/last_moment"]


def test_writer_handoff_separates_whole_answer_and_selected_final_moment():
    source = selected()
    story = narrative_decisions(source, projection_version=V9)
    assert story["episode_answer"] == source["plan"]["working_answer"]
    assert story["ending"] == source["plan"]["ending"]
    assert "conclusion" not in story
    assert story["authority"] == "revisable_authorial_decisions"
    assert all("suggested_priority" in act and "priority" not in act for act in story["acts"])
    frame = editorial_frame(source, projection_version=V9)
    assert frame["ending"] == story["ending"]
    legacy = fixture()[2]
    assert narrative_decisions(legacy, projection_version=V9)["ending"] is None
    assert "conclusion" in narrative_decisions(legacy, projection_version=V8)


def test_two_revisions_use_the_current_composition_and_never_restore_the_initial_ending():
    materials, _, base, _, original, reader, fact, _ = fixture()
    script = deepcopy(original)
    first = deepcopy(script)
    first["utterances"][0]["text"] += " CURRENT_LAST_MOMENT_ONE"
    state = composition()
    state["decisions"] = ["End at CURRENT_LAST_MOMENT_ONE."]
    selection = dict(mode="structural", script_version=0, targets=[], sequences=[])
    revised, carried, _ = script_revision.apply(
        selection,
        dict(script=first, composition=state),
        script=script,
        composition=None,
        speakers=materials.speaker_labels,
        expected_hash=digest(script),
    )
    readings = dict(reader=reader, fact=fact)
    frame = reading_payload(
        "revise",
        materials,
        script=revised,
        readings=readings,
        decision={"recommendation": "revise"},
        approved_plan=selected(base),
        projection_version=V9,
        composition=carried,
    )["editorial_frame"]
    assert "INITIAL_LAST_MOMENT" not in json.dumps(frame)
    assert "CURRENT_LAST_MOMENT_ONE" in json.dumps(frame)
    second_state = composition()
    second_state["decisions"] = ["End at CURRENT_LAST_MOMENT_TWO."]
    second = deepcopy(revised)
    second["utterances"][0]["text"] = second["utterances"][0]["text"].replace("ONE", "TWO")
    again, latest, _ = script_revision.apply(
        dict(selection, script_version=1),
        dict(script=second, composition=second_state),
        script=revised,
        composition=carried,
        speakers=materials.speaker_labels,
        expected_hash=digest(revised),
    )
    final_frame = reading_payload(
        "revise",
        materials,
        script=again,
        readings=readings,
        decision={"recommendation": "revise"},
        approved_plan=selected(base),
        projection_version=V9,
        composition=latest,
    )["editorial_frame"]
    assert "CURRENT_LAST_MOMENT_TWO" in json.dumps(final_frame)
    assert "CURRENT_LAST_MOMENT_ONE" not in json.dumps(final_frame)
    assert "INITIAL_LAST_MOMENT" not in json.dumps(final_frame)


def test_fixed_plan_import_accepts_both_complete_shapes_without_downcasting():
    from podgen.writing.editorial_contract import FixedEditorialInput

    for plan in (fixture()[2], selected()):
        data = dict(
            contract="fixed-editorial-input/2",
            assignment_key="test",
            shared_input_digest="0" * 64,
            document=dict(source_path="plan.json", sha256="1" * 64),
            plan=plan,
        )
        parsed = FixedEditorialInput.model_validate(data)
        assert parsed.model_dump(mode="json") == data
        assert (
            FixedEditorialInput.model_validate_json(parsed.model_dump_json()).model_dump(mode="json") == data
        )
