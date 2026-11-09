"""Prove which parts of a scene plan cross each model boundary, including the final scene."""

from copy import deepcopy
from uuid import uuid4

import pytest
from podgen.runtime.evaluation.planning_export import render_plan
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import FixedEditorialInput
from podgen.writing.model_references import ModelReferences, bind_schema
from podgen.writing.planning_choice import SCENE_VERSION, VERSION
from podgen.writing.planning_versions import planning_model, planning_snapshot
from podgen.writing.projection_versions import V9
from pydantic import ValidationError

from tests.historical_preview import replay_preview as preview_request
from tests.planning_choice_cases import selection
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture


def scene_proposal(cards, speakers):
    timing = dict(min_seconds=40, max_seconds=65)
    return dict(
        status="ready",
        story_selection=selection(),
        plan=dict(
            main_question="Why does the bell sound twice?",
            listener_promise="Follow the ring as it crosses the yard.",
            working_answer="The wall returns the first bell's sound.",
            opening=dict(
                development="The bell rings once. A second sound follows across the yard.",
                material_refs=[cards[0]],
            ),
            acts=[
                dict(
                    act_id="yard",
                    title="At the wall",
                    development="A listener steps towards the far wall. The two sounds draw closer together.",
                    estimated_seconds_range=timing,
                    priority="essential",
                    material_refs=[cards[1]],
                )
            ],
            ending=dict(
                development="Back at the first step, the ringing parts again.", material_refs=[cards[2]]
            ),
            host_functions=[
                dict(
                    speaker_id=s,
                    contribution="Follow the same walk.",
                    interaction_guidance="Take the listener along.",
                    speech_register="Conversational.",
                )
                for s in speakers
            ],
            duration_strategy=dict(
                estimated_seconds_range=timing,
                assumptions=["Private timing calculation."],
                optional_material=[],
            ),
        ),
        editorial_notes=[
            dict(
                scene="opening",
                kind="editorial_choice",
                note="Private decision about the unused location.",
                material_refs=[cards[3]],
            ),
            dict(
                scene="yard",
                kind="claim_scope",
                note="The record identifies the listener's position; it gives no measured interval.",
                material_refs=[cards[4]],
            ),
        ],
        research_requests=[],
    )


def make_input():
    materials, _, _, profile, script, reader, fact, review = fixture()
    proposal = scene_proposal(materials.material_catalog, materials.speaker_labels)
    return proposal, materials, profile, script, reader, fact, review


def test_explicit_scene_contract_roundtrips_without_reinterpreting_older_plans():
    output, materials, *_ = make_input()
    output["editorial_notes"][0]["note"] = "  Decision.\n" * 10000
    refs = ModelReferences.create(materials.packet)
    for stage in ("plan", "plan-revise"):
        options = dict(stage=stage, input_contract="editorial-role-packets/1", projection_version=V9)
        stable = response_schema(**options, planning_output_contract=VERSION)
        candidate = bind_schema(response_schema(**options, planning_output_contract=SCENE_VERSION), refs)
        assert "editorial_notes" not in stable["properties"]
        assert set(candidate["$defs"]["SceneDevelopment"]["required"]) == {"development", "material_refs"}
        assert candidate["$defs"]["SceneDevelopment"]["properties"]["material_refs"]["items"]["enum"] == list(
            refs.aliases
        )
        assert "why_end_here" not in str(candidate)
        for node in [candidate, *candidate["$defs"].values()]:
            if node.get("type") == "object":
                assert set(node["properties"]) == set(node["required"])
                assert node["additionalProperties"] is False
    parsed = planning_model(V9, planning_output_contract=SCENE_VERSION).model_validate(output)
    assert planning_snapshot(output).model_dump(mode="json") == parsed.model_dump(mode="json") == output
    assert refs.translate(refs.translate(output), inbound=True) == output
    fixed = dict(
        contract="fixed-editorial-input/2",
        assignment_key="fixture",
        shared_input_digest="f" * 64,
        document=dict(source_path="plan.json", sha256="e" * 64),
        plan=output,
    )
    assert FixedEditorialInput.model_validate(fixed).model_dump(mode="json") == fixed
    parsed.validate_against(
        material_locators=materials.material_catalog, speaker_ids=materials.speaker_labels
    )
    text = render_plan(dict(output=output), None, True, "en")
    for row in output["editorial_notes"]:
        assert row["note"] in text
    assert output["plan"]["ending"]["development"] in text


def test_invalid_or_unbound_notes_and_ending_sources_cannot_be_accepted():
    output, materials, *_ = make_input()
    for patch in (dict(scene="ghost"), dict(material_refs=[])):
        wrong = deepcopy(output)
        wrong["editorial_notes"][1].update(patch)
        with pytest.raises(ValidationError):
            planning_snapshot(wrong)
    for target in ("ending", "note"):
        wrong = deepcopy(output)
        row = wrong["plan"]["ending"] if target == "ending" else wrong["editorial_notes"][1]
        row["material_refs"] = ["unknown"]
        with pytest.raises(ValueError, match="planning_unknown_material_locator"):
            planning_snapshot(wrong).validate_against(
                material_locators=materials.material_catalog, speaker_ids=materials.speaker_labels
            )
    blocked = dict(
        status="needs_research",
        plan=None,
        story_selection=None,
        editorial_notes=[],
        research_requests=[
            dict(
                question="Who observed it?",
                why_needed="Identifies the observer.",
                affected_act_ids=[],
                material_refs=[],
                blocking=True,
            )
        ],
    )
    assert planning_snapshot(blocked).model_dump(mode="json") == blocked


def test_review_receives_addressed_reasoning_and_exact_evidence_even_when_author_does_not():
    output, materials, profile, script, reader, fact, review = make_input()
    choice, scope = (row["note"] for row in output["editorial_notes"])
    for stage, kwargs in [("plan-review", {}), ("plan-revise", dict(plan_review=review))]:
        packet = payload(preview_request(uuid4(), stage, profile, materials, candidate=output, **kwargs))
        assert [n["note"] for n in packet["candidate_plan"]["editorial_notes"]] == [choice, scope]
        assert packet["note_evidence"] and all(
            {"M4", "M5"}.intersection(q["supports"]) for q in packet["note_evidence"]
        )
        if stage == "plan-review":
            assert "/editorial_notes/1/note" in packet["allowed_review_targets"]
    for stage, kwargs in [
        ("draft", {}),
        ("revise-1", dict(draft=script, reader_feedback=reader, fact_feedback=fact)),
        ("revise", dict(draft=script, feedback=reader)),
    ]:
        if stage == "revise":
            kwargs["feedback"] = dict(reader, reading_mode="informed")
        packet = payload(preview_request(uuid4(), stage, profile, materials, approved_plan=output, **kwargs))
        narrative = packet["story"] if stage == "draft" else packet["editorial_frame"]["accepted_plan_scenes"]
        assert [
            n["development"] for n in (narrative["opening"], *narrative["acts"], narrative["ending"])
        ] == [
            n["development"]
            for n in (output["plan"]["opening"], *output["plan"]["acts"], output["plan"]["ending"])
        ]
        assert [
            n["support_refs"] for n in (narrative["opening"], *narrative["acts"], narrative["ending"])
        ] == [["M1"], ["M2"], ["M3"]]
        assert {c["ref"] for c in packet["support"]} == {"M1", "M2", "M3"}
        assert all(note not in repr(packet) for note in (choice, scope, "Private timing calculation."))
    factual = payload(
        preview_request(uuid4(), "fact-review", profile, materials, draft=script, approved_plan=output)
    )
    assert factual["factual_remit"]["claims_to_check"] == [dict(claim_scope=scope, material_refs=["M5"])]
    assert choice not in repr(factual) and output["plan"]["opening"]["development"] not in repr(factual)
    cold = payload(preview_request(uuid4(), "reader-review", profile, materials, draft=script))
    assert choice not in repr(cold) and scope not in repr(cold)
