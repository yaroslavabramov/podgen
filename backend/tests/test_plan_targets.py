"""Strict review context is shared; only explicitly selected broken addresses can change."""

import pytest

from podgen.domain.errors import ContractError
from podgen.writing.plan_targets import PlanTargetChoices, PlanTargetRepair, allowed_review_targets
from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput
from tests.own_plan_provider import proposal, question


def documents():
    plan = PlanningOutput.model_validate(
        proposal(dict(material_catalog=["G1"], speaker_labels=["host"]), "plan")
    )
    review = PlanReviewOutput(
        decision="accept",
        decision_reason="SYNTHETIC decision.",
        listener_answer="SYNTHETIC answer.",
        important_limits=["SYNTHETIC full limit."],
        strengths=[
            dict(
                targets=["/candidate_plan/plan/route_summary", "/plan/acts/0/title"],
                reason="SYNTHETIC connected route.",
            )
        ],
        findings=[
            dict(
                targets=["/candidate_plan/plan/limitations"],
                priority="optional",
                observation="SYNTHETIC limitation.",
                requested_change="SYNTHETIC retain limit.",
                preserve=["SYNTHETIC useful distinction."],
                material_refs=["G1"],
            )
        ],
        research_requests=[question(False)],
        duration_note="SYNTHETIC unmeasured.",
    )
    return plan, review


def test_selector_catalog_escapes_exact_document_keys_and_uses_existing_nodes_only():
    assert allowed_review_targets({"a/b": {"~key": [None, {}]}}) == [
        "/a~1b",
        "/a~1b/~0key",
        "/a~1b/~0key/0",
        "/a~1b/~0key/1",
    ]


def test_valid_context_does_not_make_invalid_addresses_valid_and_apply_preserves_all_judgments():
    plan, review = documents()
    before = review.model_dump(mode="json")
    review.validate_context(plan, material_locators=["G1"])
    with pytest.raises(ValueError, match="planning_review_unknown_target"):
        review.validate_against(plan, material_locators=["G1"])
    repair = PlanTargetRepair.build(plan, review, ["G1"])
    assert len(repair.invalid) == 2
    assert review.model_dump(mode="json") == before
    choices = PlanTargetChoices(
        choices=[
            dict(target_id=repair.invalid[0].target_id, selected_path="/plan/route_summary"),
            dict(target_id=repair.invalid[1].target_id, selected_path="/limitations"),
        ]
    )
    fixed = repair.apply(choices)
    expected = review.model_dump(mode="json")
    expected["strengths"][0]["targets"][0] = "/plan/route_summary"
    expected["findings"][0]["targets"][0] = "/limitations"
    assert fixed.model_dump(mode="json") == expected
    assert review.model_dump(mode="json") == before
    fixed.validate_against(plan, material_locators=["G1"])


def test_non_address_binding_errors_prevent_selection_instead_of_reimplementing_acceptance():
    plan, review = documents()
    with pytest.raises(ValueError, match="planning_unknown_material_locator"):
        PlanTargetRepair.build(plan, review, [])
    blocked = PlanningOutput(
        status="needs_research", plan=None, limitations=[], research_requests=[question()]
    )
    with pytest.raises(ValueError, match="planning_accept_with_unresolved_research"):
        PlanTargetRepair.build(blocked, review, ["G1"])


def test_null_cannot_be_replaced_with_an_automatic_prefix_fix():
    plan, review = documents()
    repair = PlanTargetRepair.build(plan, review, ["G1"])
    choices = PlanTargetChoices(
        choices=[dict(target_id=item.target_id, selected_path=None) for item in repair.invalid]
    )
    with pytest.raises(ContractError, match="unresolved_plan_review_target"):
        repair.apply(choices)
    assert review.strengths[0].targets[0] == "/candidate_plan/plan/route_summary"
