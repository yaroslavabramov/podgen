"""Closing-speech intent survives every planning branch, frozen wire and author handoff."""

import hashlib
import json
from copy import deepcopy

import pytest

from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing import author_plan, ending_guidance
from podgen.writing.behavior import resolve_behavior
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import bind_schema, run_references
from podgen.writing.projections import role_payload
from podgen.writing.reading_views import reading_payload
from tests.schema_cases import without_descriptions
from tests.source_excerpt_cases import data_and_profile, release
from tests.test_planning_v9 import selected
from tests.writing_instruction_history import historical_instructions

STAGES = ("plan", "plan-revise", "plan-review", "plan-review-revised")
PREVIOUS = dict(
    plan="2219c9245f9efe29c975e9ba6c89eb915fcd889f7427a49fa224b4403bf650cc",
    **{
        "plan-revise": "1eb31ffa13c7d74da40dbff8754d493249ccc77cbcde150ecf5c4ba5b50c9fd7",
        "plan-review-solo": "b042b1e96d0e77f838984b03ac4f74012834ac73a20802f00c80a64fc1355014",
        "plan-review-dialogue": "a61d3abe67d5d519802af12daaa59c014dbba6e0e55a426d9acfcde824b36935",
    },
)


@pytest.mark.parametrize(
    "pin,planner,reviewer",
    [
        (
            None,
            "8746eca1a39ca3b89b36e991be820289e04cd302b89c0c8b360bb67660705d7e",
            "f8ea042e42f9bac919af011141c48dbb774da1548f3b3dedebfc32420e6d6337",
        ),
        (
            ending_guidance.LEGACY_VERSION,
            "82436b245feeb65eebc7aa4058f6bf800eb399bb0845128ca47fe044fc45715c",
            "65ac607314ea78f22b941ece28934b4468008613640a921983f66313296ccf36",
        ),
        (
            ending_guidance.SPEECH_VERSION,
            "7d6f072d6c8736a27e99249605b01cc20ddccd6daab4cb78fe8999dc70242556",
            "b55eef856ef22a1ee5a56cd1f24f8c208b00849089032ccdd9df52ca30302262",
        ),
        (
            ending_guidance.NEUTRAL_VERSION,
            "49e5dadccaffeca2b1486d697d9800ed38b416afd347b4132fff7376eb8b286b",
            "b55eef856ef22a1ee5a56cd1f24f8c208b00849089032ccdd9df52ca30302262",
        ),
        (
            ending_guidance.CONTEXT_VERSION,
            "faa2e19267958c58dd8486b7e8aa7bd14c92509f5aad48c50336f8088f11275b",
            "6e230eb20194dab9361fa5822719e669a7069a545e56f8b20f664fb2317030a9",
        ),
    ],
)
def test_historical_full_schemas_and_field_order_remain_exact(pin, planner, reviewer):
    for stage in STAGES:
        old = response_schema(
            stage,
            "editorial-role-packets/1",
            planning_ending_guidance=pin,
            planning_output_contract="planning-story-choice/1",
            plan_review_schema_version="plan-review-preserve/1",
        )
        assert hashlib.sha256(
            json.dumps(old, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest() == (reviewer if stage.startswith("plan-review") else planner)
        current = response_schema(
            stage,
            "editorial-role-packets/1",
            planning_ending_guidance=ending_guidance.VERSION,
            planning_output_contract="planning-story-choice/1",
            plan_review_schema_version="plan-review-preserve/1",
        )
        assert json.dumps(without_descriptions(current)) == json.dumps(without_descriptions(old))


@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_full_requests_replay_all_planning_branches_without_changing_payload(monkeypatch, format):
    materials, source, _, _, _, _, _, review, profile = data_and_profile()
    candidate = selected(source)
    if format == "dialogue":
        raw = materials.packet.model_dump(mode="json")
        raw["assignment"]["format"] = format
        raw["assignment"]["speakers"].append(
            dict(
                label="СОБЕСЕДНИК", function="Ask about the investigation.", speech_register="Conversational."
            )
        )
        materials = EditorialMaterials(EditorialInput.model_validate(raw), materials.shared_ref, ())
        host = deepcopy(candidate["plan"]["host_functions"][0])
        host["speaker_id"] = "СОБЕСЕДНИК"
        candidate["plan"]["host_functions"].append(host)
    _, _, command, manifest = release(monkeypatch, materials, profile)
    references = run_references(materials, manifest)
    old = deepcopy(manifest)
    old[ending_guidance.KEY] = ending_guidance.LEGACY_VERSION
    assert manifest["reader_review_schema_version"] == resolve_behavior().reader_schema
    for stage in STAGES:
        role = "plan-review" if stage.startswith("plan-review") else stage
        identity = PREVIOUS[role + "-" + format if role == "plan-review" else role]
        previous = historical_instructions(identity)
        assert hashlib.sha256(previous.encode()).hexdigest() == identity
        current = resolve_behavior().instructions(role, format).body
        old["response_schemas"][stage] = bind_schema(
            response_schema(
                stage,
                command.input_contract,
                planning_ending_guidance=ending_guidance.LEGACY_VERSION,
                plan_review_schema_version=manifest.get("plan_review_schema_version"),
                planning_output_contract=manifest.get("planning_output_contract"),
            ),
            references,
        )
        inputs = {} if stage == "plan" else dict(candidate=candidate)
        if stage == "plan-revise":
            inputs["plan_review"] = review
        saved = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(old, previous),
            **inputs,
        )
        fresh = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(manifest, current),
            **inputs,
        )
        assert saved["instructions"] == previous and fresh["instructions"] == current
        assert fresh["instructions"] != previous
        fresh["instructions"] = previous
        fresh["text"]["format"]["schema"] = saved["text"]["format"]["schema"]
        assert fresh == saved


def test_final_act_and_complete_closing_intent_reach_both_author_packets_losslessly():
    materials, source, _, _, script, reader, fact, _, _ = data_and_profile()
    candidate = selected(source)
    candidate["plan"]["ending"] = dict(
        last_moment="The concluding words summarize the complete selected account within its final scene.\n"
        * 100,
        why_end_here="This final scene also closes the whole account; an added afterword is unnecessary.",
        promises_to_close=["The selected question"],
    )
    act = candidate["plan"]["acts"][-1]
    act["intended_change_in_understanding"] = "The final act develops its own observation and explanation."
    act["transitions"]["toward_next"] = "Its own last words realize the closing intent about the whole story."
    frozen = deepcopy(candidate)
    args = dict(approved_plan=candidate, author_plan_handoff=author_plan.VERSION)
    draft = role_payload("draft", materials, **args)
    rewrite = reading_payload(
        "revise",
        materials,
        script=script,
        readings=dict(reader=reader, fact=fact),
        decision=dict(recommendation="revise"),
        **args,
    )
    for story in (draft["story"], rewrite["editorial_frame"]):
        assert story["ending"] == candidate["plan"]["ending"]
        assert (
            story["acts"][-1]["intended_change_in_understanding"] == act["intended_change_in_understanding"]
        )
        assert story["acts"][-1]["transitions"] == act["transitions"]
    assert candidate == frozen
