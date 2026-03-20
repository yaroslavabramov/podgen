from app.pipeline.phase5_planning.act_planner import _branch_focus_summary, _build_act_prompt_payload


def test_build_act_prompt_payload_carries_story_contract_and_branch_focus() -> None:
    act = {
        "act_number": 2,
        "story_role": "explain",
        "title": "How the machine holds together",
        "purpose": "Explain the mechanism and remove the mystery from the system.",
        "opening_question": "What stops private note issuance from spinning out of control?",
        "ending_turn": "The listener sees that trust comes from the rules, not the logos on the notes.",
        "focus_branch_ids": ["b3", "b2"],
        "estimated_duration_seconds": 480,
        "content_quotas": {"verified_facts": 5},
    }
    act_claims = [
        {"id": "c1", "branch_ids": ["b3"], "statement": "Claim 1"},
        {"id": "c2", "branch_ids": ["b3", "b2"], "statement": "Claim 2"},
        {"id": "c3", "branch_ids": ["b2"], "statement": "Claim 3"},
    ]
    act_evidence = [{"id": "e1"}, {"id": "e2"}]

    payload = _build_act_prompt_payload(
        act=act,
        act_number=2,
        act_claims=act_claims,
        act_evidence=act_evidence,
    )

    assert payload["story_role"] == "explain"
    assert payload["purpose"].startswith("Explain the mechanism")
    assert payload["opening_question"].startswith("What stops private note issuance")
    assert payload["ending_turn"].startswith("The listener sees")
    assert payload["focus_branch_ids"] == ["b3", "b2"]
    assert payload["branch_focus_summary"] == [
        {"branch_id": "b2", "claim_count": 2},
        {"branch_id": "b3", "claim_count": 2},
    ]
    assert payload["claims"] == act_claims
    assert payload["evidence"] == act_evidence


def test_branch_focus_summary_sorts_by_density_then_branch_id() -> None:
    summary = _branch_focus_summary(
        [
            {"branch_ids": ["b4"]},
            {"branch_ids": ["b1", "b4"]},
            {"branch_ids": ["b1"]},
            {"branch_ids": ["b3"]},
        ]
    )

    assert summary == [
        {"branch_id": "b1", "claim_count": 2},
        {"branch_id": "b4", "claim_count": 2},
        {"branch_id": "b3", "claim_count": 1},
    ]
