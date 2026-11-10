"""Only planner field tasks change; reviewer, public shape and saved /2 requests are immutable."""

from copy import deepcopy
from pathlib import Path

import pytest
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing import ending_guidance
from podgen.writing.behavior import resolve_behavior
from podgen.writing.model_references import bind_schema, run_references
from podgen.writing.planner_ending_descriptions import FIELDS, PLAN_FIELDS

from tests.source_excerpt_cases import data_and_profile, release
from tests.test_planning_v9 import selected


def schema(stage, version):
    return response_schema(
        stage,
        "editorial-role-packets/1",
        planning_ending_guidance=version,
        planning_output_contract="planning-story-choice/1",
        plan_review_schema_version="plan-review-preserve/1",
    )


def test_only_five_planner_descriptions_change_and_every_other_role_stays_exact():
    for stage in ("plan", "plan-revise"):
        previous = schema(stage, ending_guidance.SPEECH_VERSION)
        current = schema(stage, ending_guidance.NEUTRAL_VERSION)
        expected = deepcopy(previous)
        for field, description in FIELDS.items():
            expected["$defs"]["PlanningFinalMoment"]["properties"][field]["description"] = description
        for field, description in PLAN_FIELDS.items():
            expected["$defs"]["PlanProposalV9"]["properties"][field]["description"] = description
        assert current == expected
        assert all(
            current["$defs"]["PlanningFinalMoment"]["properties"][field]["description"] == description
            for field, description in FIELDS.items()
        )
    for stage in ("plan-review", "plan-review-revised", "draft", "revise", "fact-review", "reader-review"):
        assert schema(stage, ending_guidance.SPEECH_VERSION) == schema(stage, ending_guidance.NEUTRAL_VERSION)


@pytest.mark.parametrize("stage", ["plan", "plan-revise"])
def test_real_planner_and_rewrite_previews_keep_previous_schema_and_prompt_when_pinned(monkeypatch, stage):
    materials, plan, _, _, _, _, _, review, profile = data_and_profile()
    _, _, command, manifest = release(monkeypatch, materials, profile)
    assert manifest[ending_guidance.KEY] == ending_guidance.VERSION
    old = deepcopy(manifest)
    old[ending_guidance.KEY] = ending_guidance.SPEECH_VERSION
    old["response_schemas"][stage] = bind_schema(
        schema(stage, ending_guidance.SPEECH_VERSION), run_references(materials, manifest)
    )
    previous_sha = {
        "plan": "a3634c8af3c1a83c4a3e5b97c4b46cc72ba041953c9d94163bba16c4ec23467c",
        "plan-revise": "12a11b1988cdae45d68be3690cf336a3a718474f97deaef85ce9fde64f3abbc7",
    }[stage]
    previous = (
        Path(__file__).parents[1] / f"src/podgen/writing/behavior/assets/{previous_sha}.md"
    ).read_text()
    instructions = resolve_behavior().instructions(stage, materials.format).body
    inputs = {} if stage == "plan" else dict(candidate=selected(plan), plan_review=review)
    saved = preview_request(
        command.run_id, stage, profile(stage), materials, snapshot=PreviewSnapshot(old, previous), **inputs
    )
    fresh = preview_request(
        command.run_id,
        stage,
        profile(stage),
        materials,
        snapshot=PreviewSnapshot(manifest, instructions),
        **inputs,
    )
    assert saved["instructions"] == previous and fresh["instructions"] == instructions
    ending = fresh["text"]["format"]["schema"]["$defs"]["PlanningFinalMoment"]["properties"]
    from podgen.writing.compact_editorial_descriptions import PLANNER_ENDING

    assert all(ending[field]["description"] == description for field, description in PLANNER_ENDING.items())
    fresh["instructions"] = previous
    fresh["text"]["format"]["schema"] = saved["text"]["format"]["schema"]
    assert fresh == saved
