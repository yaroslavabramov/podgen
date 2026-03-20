from app.pipeline.phase5_planning.act_planner import (
    ACT_PLANNER_SYSTEM,
    _branch_focus_summary,
    _build_act_prompt_payload,
    _extract_scene_strategy,
    _scene_count_guidance,
)


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
    assert payload["scene_count_guidance"] == {"min_scenes": 5, "target_scenes": 6, "max_scenes": 7}
    assert payload["branch_focus_summary"] == [
        {"branch_id": "b2", "claim_count": 2},
        {"branch_id": "b3", "claim_count": 2},
    ]
    assert payload["conflict_brief_ids"] == []
    assert payload["conflict_count"] == 0
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


def test_scene_count_guidance_tracks_duration_bands() -> None:
    assert _scene_count_guidance(180) == {"min_scenes": 3, "target_scenes": 4, "max_scenes": 5}
    assert _scene_count_guidance(300) == {"min_scenes": 4, "target_scenes": 5, "max_scenes": 6}
    assert _scene_count_guidance(420) == {"min_scenes": 5, "target_scenes": 6, "max_scenes": 7}
    assert _scene_count_guidance(600) == {"min_scenes": 6, "target_scenes": 7, "max_scenes": 8}


def test_act_planner_prompt_demands_compact_beats_and_real_conflict_gate() -> None:
    assert '"scene_strategy": "short phrase describing the act\'s internal movement"' in ACT_PLANNER_SYSTEM
    assert "first scene must explicitly open the act's `opening_question`" in ACT_PLANNER_SYSTEM
    assert "final scene must clearly deliver the act's `ending_turn`" in ACT_PLANNER_SYSTEM
    assert "Prefer compact beat cards, not mini-script paragraphs." in ACT_PLANNER_SYSTEM
    assert "Use `debate` only when the act has real conflict pressure" in ACT_PLANNER_SYSTEM
    assert "Use `scene_count_guidance` as the default size target" in ACT_PLANNER_SYSTEM
    assert "Act-to-act variety matters. Do not default every act to the same internal shape." in ACT_PLANNER_SYSTEM
    assert "Each act should contain at least one meaningful turn" in ACT_PLANNER_SYSTEM
    assert "Avoid the default podcast skeleton of hook -> explanation -> case_study -> takeaway" in ACT_PLANNER_SYSTEM
    assert "A `case_study` scene is optional, not mandatory." in ACT_PLANNER_SYSTEM


def test_extract_scene_strategy_reads_top_level_field() -> None:
    assert _extract_scene_strategy({"scene_strategy": "paradox -> failed fix -> pragmatic turn"}) == (
        "paradox -> failed fix -> pragmatic turn"
    )
    assert _extract_scene_strategy({"scene_strategy": "   "}) == ""
    assert _extract_scene_strategy({"beat_sheet": []}) == ""
