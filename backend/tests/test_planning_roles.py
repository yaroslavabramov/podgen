import copy
import json
import re

import pytest
from pydantic import ValidationError

from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput


@pytest.fixture
def proposal_payload():
    seconds = {"min_seconds": 30, "max_seconds": 60}
    return {
        "status": "ready",
        "plan": {
            "main_question": "What does the procedure establish?",
            "listener_promise": "Understand the procedure and its limits.",
            "working_answer": "The record establishes a sequence, not its historical motive.",
            "route_summary": "Follow an application through the recorded steps and separate outcome from motive.",
            "opening": {
                "approach": "Present the documented application.",
                "material_refs": ["evidence-A"],
                "question_to_open": "What can happen next?",
            },
            "acts": [
                {
                    "act_id": "a1",
                    "title": "Follow the application",
                    "listener_question": "Which step changes the application's state?",
                    "intended_change_in_understanding": "Distinguish a required action from its possible motive.",
                    "prerequisite_concepts": [],
                    "material_refs": ["evidence-A"],
                    "limitations_to_voice": ["The record does not explain the historical motive."],
                    "transitions": {
                        "from_previous": "Use the opening application to follow the recorded steps.",
                        "toward_next": "Carry the established sequence and its limit into the answer.",
                    },
                    "promises_opened": [],
                    "promises_addressed": ["Explain the procedure."],
                    "estimated_seconds_range": seconds,
                    "priority": "essential",
                }
            ],
            "ending": {
                "answer_to_deliver": "The sequence is documented; its historical motive remains unknown.",
                "qualifications": ["Do not infer motive from sequence."],
                "promises_to_close": ["Explain the procedure."],
            },
            "host_functions": [
                {
                    "speaker_id": "host_a",
                    "contribution": "Follow the application and test what the record supports.",
                    "interaction_guidance": "Develop the explanation as a solo narrator.",
                    "speech_register": "Clear conversational English.",
                }
            ],
            "duration_strategy": {
                "estimated_seconds_range": seconds,
                "assumptions": ["Estimate only; includes the opening and ending."],
                "optional_material": [],
            },
        },
        "limitations": ["The historical motive is outside the supported answer."],
        "research_requests": [],
    }


@pytest.fixture
def review_payload():
    return {
        "decision": "accept",
        "decision_reason": "The supported answer matches the promise.",
        "listener_answer": "The procedure is explained without inventing a motive.",
        "important_limits": ["The motive is unknown."],
        "strengths": [{"targets": ["/plan/route_summary"], "reason": "A connected explanatory route."}],
        "findings": [],
        "research_requests": [],
        "duration_note": "The range is an estimate, not measured audio.",
    }


def request(*, blocking=True, acts=()):
    return {
        "question": "What evidence establishes the missing step?",
        "why_needed": "Without it the promised causal explanation is unsupported.",
        "affected_act_ids": list(acts),
        "material_refs": [],
        "blocking": blocking,
    }


def test_complete_plan_and_review_bind_to_the_supplied_inputs(proposal_payload, review_payload):
    proposal = PlanningOutput.model_validate(proposal_payload)
    proposal.validate_against(material_locators={"evidence-A"}, speaker_ids={"host_a"})
    review = PlanReviewOutput.model_validate(review_payload)
    review.validate_against(proposal, material_locators={"evidence-A"})
    assert PlanningOutput.model_validate_json(proposal.model_dump_json()) == proposal
    assert PlanReviewOutput.model_validate_json(review.model_dump_json()) == review


def test_empty_corpus_preserves_a_blocker_without_inventing_a_plan():
    proposal = PlanningOutput(
        status="needs_research",
        plan=None,
        limitations=["No supporting material supplied."],
        research_requests=[request()],
    )
    proposal.validate_against(material_locators=set(), speaker_ids={"host_a", "host_b"})
    restored = PlanningOutput.model_validate_json(proposal.model_dump_json())
    assert restored.plan is None and restored.research_requests[0].blocking


@pytest.mark.parametrize("state", ["ready_without_plan", "ready_with_blocker", "research_without_blocker"])
def test_contradictory_planning_states_fail_explicitly(proposal_payload, state):
    if state == "ready_without_plan":
        proposal_payload["plan"] = None
    elif state == "ready_with_blocker":
        proposal_payload["research_requests"] = [request(acts=["a1"])]
    else:
        proposal_payload["status"] = "needs_research"
    with pytest.raises(ValidationError, match="planning_"):
        PlanningOutput.model_validate(proposal_payload)


def test_explainable_uncertainty_and_optional_questions_do_not_force_research(
    proposal_payload, review_payload
):
    proposal_payload["research_requests"] = [request(blocking=False, acts=["a1"])]
    proposal = PlanningOutput.model_validate(proposal_payload)
    review_payload["research_requests"] = [request(blocking=False, acts=["a1"])]
    PlanReviewOutput.model_validate(review_payload).validate_against(
        proposal, material_locators={"evidence-A"}
    )


@pytest.mark.parametrize("field", ["act_id", "speaker_id"])
def test_duplicate_local_ids_are_rejected(proposal_payload, field):
    group = "acts" if field == "act_id" else "host_functions"
    proposal_payload["plan"][group] *= 2
    with pytest.raises(ValidationError, match="planning_duplicate_"):
        PlanningOutput.model_validate(proposal_payload)


@pytest.mark.parametrize("bad_value", [-1, True, "30"])
def test_estimates_require_nonnegative_integer_seconds(proposal_payload, bad_value):
    proposal_payload["plan"]["acts"][0]["estimated_seconds_range"]["min_seconds"] = bad_value
    with pytest.raises(ValidationError):
        PlanningOutput.model_validate(proposal_payload)


def test_reversed_duration_range_and_missing_complete_structure_fail(proposal_payload):
    invalid = copy.deepcopy(proposal_payload)
    invalid["plan"]["duration_strategy"]["estimated_seconds_range"] = {"min_seconds": 60, "max_seconds": 30}
    with pytest.raises(ValidationError, match="planning_seconds_range_reversed"):
        PlanningOutput.model_validate(invalid)
    proposal_payload["plan"]["acts"] = []
    with pytest.raises(ValidationError):
        PlanningOutput.model_validate(proposal_payload)


@pytest.mark.parametrize("site", ["opening", "act", "request"])
def test_material_addresses_must_be_explicitly_offered(proposal_payload, site):
    if site == "request":
        proposal_payload["research_requests"] = [request(blocking=False)]
        target = proposal_payload["research_requests"][0]
    else:
        target = proposal_payload["plan"]["opening" if site == "opening" else "acts"]
        if site == "act":
            target = target[0]
    target["material_refs"] = ["unadmitted/document.txt"]
    proposal = PlanningOutput.model_validate(proposal_payload)
    with pytest.raises(ValueError, match="planning_unknown_material_locator"):
        proposal.validate_against(material_locators={"evidence-A"}, speaker_ids={"host_a"})
    proposal.validate_against(
        material_locators={"evidence-A", "unadmitted/document.txt"}, speaker_ids={"host_a"}
    )


def test_assigned_speaker_ids_and_material_labels_are_exact(proposal_payload):
    proposal = PlanningOutput.model_validate(proposal_payload)
    with pytest.raises(ValueError, match="planning_speaker_assignment_mismatch"):
        proposal.validate_against(material_locators={"evidence-A"}, speaker_ids={"host_b"})
    with pytest.raises(ValueError, match="planning_unknown_material_locator"):
        proposal.validate_against(material_locators={"evidence-a"}, speaker_ids={"host_a"})


@pytest.mark.parametrize("decision", ["accept", "revise"])
def test_blocking_review_question_cannot_be_reported_as_a_different_decision(review_payload, decision):
    review_payload.update(decision=decision, research_requests=[request()])
    with pytest.raises(ValidationError, match="planning_research_decision_mismatch"):
        PlanReviewOutput.model_validate(review_payload)


def test_research_decision_requires_a_preserved_blocking_question(review_payload):
    review_payload["decision"] = "needs_research"
    with pytest.raises(ValidationError, match="planning_research_decision_mismatch"):
        PlanReviewOutput.model_validate(review_payload)


def test_an_existing_blocked_proposal_cannot_receive_accept(review_payload):
    blocked = PlanningOutput(
        status="needs_research", plan=None, limitations=[], research_requests=[request()]
    )
    review = PlanReviewOutput.model_validate(review_payload)
    review = review.model_copy(update={"strengths": []})
    with pytest.raises(ValueError, match="planning_accept_with_unresolved_research"):
        review.validate_against(blocked, material_locators=set())
    review_payload.update(decision="needs_research", strengths=[], research_requests=[request()])
    PlanReviewOutput.model_validate(review_payload).validate_against(blocked, material_locators=set())


@pytest.mark.parametrize(
    "target", ["plan/acts", "/plan/acts/1", "/plan/acts/-1", "/plan/acts/00", "/bad~2key"]
)
def test_review_targets_must_resolve_in_the_exact_supplied_plan(proposal_payload, review_payload, target):
    review_payload["strengths"][0]["targets"] = [target]
    with pytest.raises(ValueError, match="planning_review_"):
        PlanReviewOutput.model_validate(review_payload).validate_against(
            PlanningOutput.model_validate(proposal_payload), material_locators={"evidence-A"}
        )


def test_actionable_revision_binds_target_and_support(proposal_payload, review_payload):
    review_payload.update(
        decision="revise",
        findings=[
            {
                "targets": ["/plan/acts/0/transitions/toward_next"],
                "priority": "important",
                "observation": "The transition does not explain the next step.",
                "requested_change": "Connect the recorded action to its limited consequence.",
                "preserve": ["Keep the documented observation."],
                "material_refs": ["evidence-A"],
            }
        ],
    )
    review = PlanReviewOutput.model_validate(review_payload)
    review.validate_against(PlanningOutput.model_validate(proposal_payload), material_locators={"evidence-A"})
    with pytest.raises(ValueError, match="planning_unknown_material_locator"):
        review.validate_against(PlanningOutput.model_validate(proposal_payload), material_locators=set())
    review_payload["decision"] = "accept"
    with pytest.raises(ValidationError, match="planning_accept_with_required_changes"):
        PlanReviewOutput.model_validate(review_payload)


def test_research_questions_bind_to_existing_local_acts(proposal_payload, review_payload):
    proposal_payload["research_requests"] = [request(blocking=False, acts=["missing"])]
    with pytest.raises(ValidationError, match="planning_unknown_research_act"):
        PlanningOutput.model_validate(proposal_payload)
    proposal_payload["research_requests"] = []
    review_payload.update(decision="needs_research", research_requests=[request(acts=["missing"])])
    with pytest.raises(ValueError, match="planning_unknown_research_act"):
        PlanReviewOutput.model_validate(review_payload).validate_against(
            PlanningOutput.model_validate(proposal_payload), material_locators={"evidence-A"}
        )


def test_full_meaning_and_whitespace_survive_persistence(proposal_payload, review_payload):
    full = "  Начало ё 😀 е\u0301.\n" + "A supported explanation.\n" * 20_000 + "Конец.  "
    proposal_payload["plan"]["route_summary"] = full
    review_payload["decision_reason"] = full
    for model, payload, value in (
        (PlanningOutput, proposal_payload, lambda m: m.plan.route_summary),
        (PlanReviewOutput, review_payload, lambda m: m.decision_reason),
    ):
        restored = model.model_validate_json(model.model_validate(payload).model_dump_json())
        assert value(restored) == full


@pytest.mark.parametrize("model", [PlanningOutput, PlanReviewOutput])
def test_wire_schemas_are_closed_required_and_without_semantic_text_caps(model):
    schema = model.model_json_schema()
    for definition in [schema, *schema.get("$defs", {}).values()]:
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])
    assert "maxLength" not in json.dumps(schema)


@pytest.mark.parametrize(
    "name,fields",
    [
        ("plan", ("assignment", "dossier")),
        ("plan-review", ("candidate_plan", "allowed_review_targets")),
        ("plan-revise", ("revision_guidance",)),
    ],
)
def test_planning_assets_match_role_specific_inputs(name, fields):
    from podgen.writing.prompts import role_instructions

    text = role_instructions(name, "solo")
    assert "output_language" in text
    assert not re.search(r"[А-Яа-яЁё]", text)
    assert not re.search(r"\b[0-9a-f]{64}\b", text)
    for field in fields:
        assert field in text
