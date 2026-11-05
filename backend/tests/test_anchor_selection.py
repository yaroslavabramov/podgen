import pytest

from podgen.domain.errors import ContractError
from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan
from podgen.writing.roles import EditorialFeedback, WriteOutput


@pytest.fixture
def plan():
    draft = WriteOutput.model_validate(
        {
            "utterances": [
                {"speaker": "А", "text": "Один факт. Второй факт 🛰️."},
                {"speaker": "Б", "text": "Кому это принадлежит? Автору."},
            ]
        }
    )
    review = EditorialFeedback.model_validate(
        {
            "reading_mode": "informed",
            "listener_answer": "Ответ",
            "important_limits": [],
            "strengths": [
                {
                    "anchors": [
                        {"utterance_index": 0, "quote": "Один факт."},
                        {"utterance_index": 0, "quote": "Второй факт 🛰️:"},
                    ],
                    "reason": "Два примера.",
                }
            ],
            "findings": [],
            "research_requests": [
                {
                    "anchors": [{"utterance_index": 99, "quote": "автору."}],
                    "question": "Кто сообщил об этом?",
                    "why_needed": "Уточнить источник.",
                }
            ],
            "duration_note": "Без читки.",
            "recommendation": "revise",
        }
    )
    return AnchorRepairPlan.build(draft, review)


def test_only_selected_invalid_anchors_change_and_every_offered_range_is_exact(plan):
    exposed = plan.as_payload()
    assert [x["anchor_id"] for x in exposed["invalid_anchors"]] == [
        "strengths/0/anchors/1",
        "research_requests/0/anchors/0",
    ]
    assert len({p.selector_id for p in plan.passages}) == len(plan.passages)
    for p in plan.passages:
        assert plan.draft.utterances[p.utterance_index].text[p.start : p.end] == p.text
    ids = {p.text: p.selector_id for p in plan.passages}
    choices = AnchorChoices(
        choices=[
            {"anchor_id": "strengths/0/anchors/1", "selector_id": ids["Второй факт 🛰️."]},
            {"anchor_id": "research_requests/0/anchors/0", "selector_id": ids["Автору."]},
        ]
    )
    original = plan.feedback.model_dump(mode="json")
    repaired = plan.apply(choices).model_dump(mode="json")
    expected = plan.feedback.model_dump(mode="json")
    expected["strengths"][0]["anchors"][1]["quote"] = "Второй факт 🛰️."
    expected["research_requests"][0]["anchors"][0] = {"utterance_index": 1, "quote": "Автору."}
    assert repaired == expected
    assert plan.feedback.model_dump(mode="json") == original


@pytest.mark.parametrize(
    ("second_target", "selector", "code"),
    [
        ("strengths/0/anchors/1", "u0:0-10", "anchor_repair_target_mismatch"),
        ("strengths/0/anchors/0", "u0:0-10", "anchor_repair_target_mismatch"),
        ("research_requests/0/anchors/0", "u5:0-42", "unknown_anchor_selector"),
        ("research_requests/0/anchors/0", None, "unresolved_editorial_anchor"),
    ],
)
def test_cannot_change_other_fields_or_fabricate_a_locator(plan, second_target, selector, code):
    choices = AnchorChoices(
        choices=[
            {"anchor_id": "strengths/0/anchors/1", "selector_id": plan.passages[0].selector_id},
            {"anchor_id": second_target, "selector_id": selector},
        ]
    )
    with pytest.raises(ContractError, match=code):
        plan.apply(choices)
