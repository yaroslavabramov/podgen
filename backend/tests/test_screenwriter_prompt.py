import json
from types import SimpleNamespace

from app.config import settings
from app.pipeline.phase6_writing.screenwriter import (
    SCREENWRITER_SYSTEM,
    _build_already_covered_claims_payload,
    _build_new_information_goal,
    _extract_last_dialogue_line,
    _extract_mechanism_anchors,
    _build_previous_act_summary,
    _build_previous_scene_payload,
    _build_scene_prompt_payload,
    _collect_manifest_claim_ids,
    _dump_screenwriter_debug,
    _extract_dialogue_claim_ids,
    _scene_manifest_claim_ids,
    _scene_primary_claim_ids,
    _should_force_rewrite_for_node_rerun,
)


def test_build_scene_prompt_payload_uses_local_and_cross_act_context() -> None:
    payload = _build_scene_prompt_payload(
        act={
            "title": "Черная суббота и привязка",
        },
        act_number=3,
        scene={
            "type": "case_study",
            "description": "Черная суббота 1983 года как момент потери доверия.",
            "transition_note": "После паники перейти к механике привязки.",
            "notes": "Один ведущий начинает с конкретной паники, второй расширяет рамку.",
            "estimated_duration_seconds": 75,
        },
        scene_number=1,
        scene_claims=[
            {
                "id": "c1",
                "type": "fact",
                "verification": "verified",
                "statement": "The peg was restored in October 1983.",
                "attribution": None,
            }
        ],
        scene_evidence=[
            {
                "id": "e1",
                "quote": "On 17 October 1983 the exchange rate was fixed at HK$7.8 to US$1.",
                "asset_type": "timeline",
                "tellability": "Anchor the panic in a concrete date.",
            }
        ],
        hosts=[{"name": "Host A", "role": "Guide"}, {"name": "Host B", "role": "Challenger"}],
        forbidden_phrases=["game changer"],
        max_reply_tokens=180,
        language="ru",
        previous_scene={
            "scene_number": 0,
            "scene_type": "hook",
            "description": "В предыдущей сцене объяснили, как появился валютный хаос.",
            "transition_note": "Теперь показать, что паника заставила систему измениться.",
        },
        previous_act_summary={
            "act_number": 2,
            "act_title": "Как устроен механизм",
            "ending_scene_type": "takeaway",
            "what_listener_now_knows": "Теперь слушатель понимает, почему банки не могут печатать без ограничений.",
            "bridge_to_current_act": "Но этот механизм пришлось строить в панике 1983 года.",
        },
        already_covered_claims=[
            {"id": "c0", "statement": "Earlier scene already established the monetary chaos."}
        ],
        already_covered_claims_global=[
            {
                "id": "g1",
                "statement": "Earlier act already established the reserve discipline.",
                "mechanism_anchors": ["7.80", "Exchange Fund"],
            }
        ],
        new_information_goal="Advance the listener with what is new here: The peg was restored in October 1983.",
    )

    assert payload == {
        "language": "ru",
        "act_number": 3,
        "act_title": "Черная суббота и привязка",
        "scene_number": 1,
        "scene_type": "case_study",
        "description": "Черная суббота 1983 года как момент потери доверия.",
        "transition_note": "После паники перейти к механике привязки.",
        "notes": "Один ведущий начинает с конкретной паники, второй расширяет рамку.",
        "estimated_duration_seconds": 75,
        "previous_scene": {
            "scene_number": 0,
            "scene_type": "hook",
            "description": "В предыдущей сцене объяснили, как появился валютный хаос.",
            "transition_note": "Теперь показать, что паника заставила систему измениться.",
        },
        "previous_act_summary": {
            "act_number": 2,
            "act_title": "Как устроен механизм",
            "ending_scene_type": "takeaway",
            "what_listener_now_knows": "Теперь слушатель понимает, почему банки не могут печатать без ограничений.",
            "bridge_to_current_act": "Но этот механизм пришлось строить в панике 1983 года.",
        },
        "already_covered_claims": [
            {"id": "c0", "statement": "Earlier scene already established the monetary chaos."}
        ],
        "already_covered_claims_global": [
            {
                "id": "g1",
                "statement": "Earlier act already established the reserve discipline.",
                "mechanism_anchors": ["7.80", "Exchange Fund"],
            }
        ],
        "new_information_goal": "Advance the listener with what is new here: The peg was restored in October 1983.",
        "claims": [
            {
                "id": "c1",
                "type": "fact",
                "verification": "verified",
                "statement": "The peg was restored in October 1983.",
                "attribution": None,
            }
        ],
        "evidence": [
            {
                "id": "e1",
                "quote": "On 17 October 1983 the exchange rate was fixed at HK$7.8 to US$1.",
                "asset_type": "timeline",
                "tellability": "Anchor the panic in a concrete date.",
            }
        ],
        "hosts": [{"name": "Host A", "role": "Guide"}, {"name": "Host B", "role": "Challenger"}],
        "forbidden_phrases": ["game changer"],
        "max_reply_tokens": 180,
    }


def test_screenwriter_prompt_demands_cross_act_memory_and_looser_dialogue() -> None:
    assert "All dialogue lines MUST be in the `language` from the user payload." in SCREENWRITER_SYSTEM
    assert "Use `description` as the main job of the scene." in SCREENWRITER_SYSTEM
    assert "previous_act_summary" in SCREENWRITER_SYSTEM
    assert "already_covered_claims_global" in SCREENWRITER_SYSTEM
    assert "mechanism_anchors" in SCREENWRITER_SYSTEM
    assert "Treat `new_information_goal` as the delta this scene must add." in SCREENWRITER_SYSTEM
    assert "Prefer callbacks over re-explanations." in SCREENWRITER_SYSTEM
    assert "Do not default every scene to strict `ABAB` alternation." in SCREENWRITER_SYSTEM
    assert "Do not default every reaction to the same opener or confirmation word." in SCREENWRITER_SYSTEM
    assert "These are soft tendencies, not rigid scene roles." in SCREENWRITER_SYSTEM
    assert "`takeaway`: land the consequence or implication, not just a generic recap" in SCREENWRITER_SYSTEM
    assert "When a precise figure, legal clause, institutional rule, or date matters" in SCREENWRITER_SYSTEM


def test_build_previous_scene_payload_returns_previous_scene_only() -> None:
    beat_sheet = [
        {"scene": 1, "type": "hook", "description": "Scene one", "transition_note": "Move to scene two"},
        {"scene": 2, "type": "explanation", "description": "Scene two", "transition_note": "Move to scene three"},
    ]

    assert _build_previous_scene_payload(beat_sheet, 1) is None
    assert _build_previous_scene_payload(beat_sheet, 2) == {
        "scene_number": 1,
        "scene_type": "hook",
        "description": "Scene one",
        "transition_note": "Move to scene two",
    }


def test_build_previous_act_summary_returns_previous_act_takeaway() -> None:
    acts_json = [
        {
            "act_number": 1,
            "title": "Origins",
            "ending_turn": "By the end of the act, the listener understands why private banks stepped in.",
            "beat_sheet": [
                {"scene": 1, "type": "hook", "description": "Opening", "transition_note": "Move on"},
                {
                    "scene": 2,
                    "type": "takeaway",
                    "description": "Now the listener knows the port was chaotic.",
                    "transition_note": "Next explain why private banks stepped in.",
                },
            ],
        },
        {"act_number": 2, "title": "Mechanism", "beat_sheet": []},
    ]

    assert _build_previous_act_summary(acts_json, 1, existing_segments={}, claim_lookup={}) is None
    assert _build_previous_act_summary(acts_json, 2, existing_segments={}, claim_lookup={}) == {
        "act_number": 1,
        "act_title": "Origins",
        "ending_scene_type": "takeaway",
        "what_listener_now_knows": "By the end of the act, the listener understands why private banks stepped in.",
        "bridge_to_current_act": "Next explain why private banks stepped in.",
    }


def test_build_previous_act_summary_prefers_written_dialogue_over_planning_metadata() -> None:
    acts_json = [
        {
            "act_number": 1,
            "title": "Origins",
            "ending_turn": "Planning-only ending turn.",
            "beat_sheet": [
                {
                    "scene": 1,
                    "type": "takeaway",
                    "description": "Planning-only description.",
                    "transition_note": "Planning-only transition note.",
                }
            ],
        },
        {"act_number": 2, "title": "Mechanism", "beat_sheet": []},
    ]
    existing_segments = {
        (1, 1): SimpleNamespace(
            dialogue_json=[
                {"speaker": "A", "text": "We already learned that private banks stepped in.", "claim_ids": ["c1"]},
                {"speaker": "B", "text": "And that is the bridge to the mechanism.", "claim_ids": []},
            ]
        )
    }
    claim_lookup = {"c1": {"statement": "Private banks stepped in because the state would not emit directly."}}

    assert _build_previous_act_summary(
        acts_json,
        2,
        existing_segments=existing_segments,
        claim_lookup=claim_lookup,
    ) == {
        "act_number": 1,
        "act_title": "Origins",
        "ending_scene_type": "takeaway",
        "what_listener_now_knows": "Private banks stepped in because the state would not emit directly.",
        "bridge_to_current_act": "And that is the bridge to the mechanism.",
    }


def test_build_already_covered_claims_payload_uses_written_claim_ids() -> None:
    claim_lookup = {
        "c1": {"statement": "First"},
        "c2": {"statement": "Second"},
        "c3": {"statement": "Third"},
    }
    prior_dialogue = [
        {"speaker": "A", "text": "One", "claim_ids": ["c1"]},
        {"speaker": "B", "text": "Two", "claim_ids": []},
    ]

    assert _build_already_covered_claims_payload(
        covered_claim_ids=_extract_dialogue_claim_ids(prior_dialogue),
        claim_lookup=claim_lookup,
    ) == [
        {"id": "c1", "statement": "First"},
    ]


def test_build_already_covered_claims_payload_can_cap_and_truncate_for_global_memory() -> None:
    claim_lookup = {
        "c1": {"statement": "First statement that should be dropped by the cap."},
        "c2": {"statement": "Second statement that should remain."},
        "c3": {"statement": "Third statement that should remain and be truncated if needed."},
    }

    assert _build_already_covered_claims_payload(
        covered_claim_ids=["c1", "c2", "c3"],
        claim_lookup=claim_lookup,
        max_items=2,
        statement_chars=10,
    ) == [
        {"id": "c2", "statement": "Second sta"},
        {"id": "c3", "statement": "Third stat"},
    ]


def test_extract_dialogue_claim_ids_dedupes_and_preserves_order() -> None:
    dialogue = [
        {"speaker": "A", "text": "One", "claim_ids": ["c1", "c2"]},
        {"speaker": "B", "text": "Two", "claim_ids": []},
        {"speaker": "A", "text": "Three", "claim_ids": ["c2", "c3"]},
        {"speaker": "B", "text": "Four"},
        {"speaker": "A", "text": "Five", "claim_ids": ["c1", "c4"]},
    ]

    assert _extract_dialogue_claim_ids(dialogue) == ["c1", "c2", "c3", "c4"]


def test_extract_last_dialogue_line_returns_last_nonempty_text() -> None:
    assert _extract_last_dialogue_line(
        [
            {"speaker": "A", "text": "First"},
            {"speaker": "B", "text": "  "},
            {"speaker": "A", "text": "Last meaningful line"},
        ]
    ) == "Last meaningful line"


def test_collect_manifest_claim_ids_dedupes_in_order() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"
    c2 = "22222222-2222-2222-2222-222222222222"
    c3 = "33333333-3333-3333-3333-333333333333"
    c4 = "44444444-4444-4444-4444-444444444444"
    acts_json = [
        {
            "beat_sheet": [
                {"primary_claim_ids": [c1], "callback_claim_ids": [c2]},
                {"claim_ids": [c2, c3]},
            ]
        },
        {"beat_sheet": [{"primary_claim_ids": [c3], "callback_claim_ids": [c4]}]},
    ]

    assert _collect_manifest_claim_ids(acts_json) == [c1, c2, c3, c4]


def test_scene_claim_helpers_prefer_primary_and_include_callbacks_for_manifest() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"
    c2 = "22222222-2222-2222-2222-222222222222"
    c3 = "33333333-3333-3333-3333-333333333333"
    scene = {
        "primary_claim_ids": [c1],
        "callback_claim_ids": [c2],
        "claim_ids": [c1, c2, c3],
    }

    assert _scene_primary_claim_ids(scene) == [c1]
    assert _scene_manifest_claim_ids(scene) == [c1, c2, c3]


def test_build_new_information_goal_prefers_uncovered_claims() -> None:
    goal = _build_new_information_goal(
        scene={"description": "Fallback description"},
        scene_claims=[
            {"id": "c1", "statement": "Already known statement."},
            {"id": "c2", "statement": "Fresh claim about reserve discipline."},
        ],
        local_covered_claim_ids={"c1"},
        global_covered_claim_ids=set(),
    )

    assert goal == "Advance the listener with what is new here: Fresh claim about reserve discipline."


def test_build_new_information_goal_falls_back_to_scene_description_when_all_claims_covered() -> None:
    goal = _build_new_information_goal(
        scene={"description": "Reframe the same mechanism as the price of stability."},
        scene_claims=[{"id": "c1", "statement": "Known statement."}],
        local_covered_claim_ids=set(),
        global_covered_claim_ids={"c1"},
    )

    assert "Do not reteach already covered setup." in goal
    assert "Reframe the same mechanism as the price of stability." in goal


def test_build_already_covered_claims_payload_can_attach_mechanism_anchors() -> None:
    payload = _build_already_covered_claims_payload(
        covered_claim_ids=["c1"],
        claim_lookup={
            "c1": {
                "statement": "HKMA requires Certificates of Indebtedness at HK$7.80 via the Exchange Fund."
            }
        },
        include_mechanism_anchors=True,
    )

    assert payload == [
        {
            "id": "c1",
            "statement": "HKMA requires Certificates of Indebtedness at HK$7.80 via the Exchange Fund.",
            "mechanism_anchors": ["HK$7.80", "HKMA", "Certificates of Indebtedness", "Exchange Fund"],
        }
    ]


def test_extract_mechanism_anchors_pulls_numbers_terms_and_institutions() -> None:
    anchors = _extract_mechanism_anchors(
        "HKMA requires Certificates of Indebtedness at HK$7.80 via the Exchange Fund under the Basic Law."
    )

    assert anchors == [
        "HK$7.80",
        "HKMA",
        "Certificates of Indebtedness",
        "Exchange Fund",
        "Basic Law",
    ]


def test_extract_mechanism_anchors_ignores_plain_years_and_counts() -> None:
    anchors = _extract_mechanism_anchors(
        "In 1842, 14 banks issued notes under the Colonial Bank Ordinance."
    )

    assert "1842" not in anchors
    assert "14" not in anchors
    assert "Colonial Bank Ordinance" in anchors


def test_dump_screenwriter_debug_writes_scene_dump(tmp_path) -> None:
    original_dir = settings.web_extractor_debug_dump_dir
    settings.web_extractor_debug_dump_dir = str(tmp_path)
    try:
        _dump_screenwriter_debug(
            run_id="run-123",
            act_number=2,
            scene_number=4,
            status="ok",
            prompt_payload={"scene_type": "explanation"},
            attempts=[{"attempt": 1, "status": "ok"}],
            dialogue=[{"speaker": "A", "text": "Пример"}],
            error=None,
        )
        dump_files = list((tmp_path / "screenwriter").glob("*.json"))
        assert len(dump_files) == 1
        payload = json.loads(dump_files[0].read_text())
        assert payload["run_id"] == "run-123"
        assert payload["act_number"] == 2
        assert payload["scene_number"] == 4
        assert payload["status"] == "ok"
        assert payload["input"] == {"scene_type": "explanation"}
        assert payload["output"]["dialogue_line_count"] == 1
    finally:
        settings.web_extractor_debug_dump_dir = original_dir


def test_should_force_rewrite_for_targeted_screenwriter_rerun() -> None:
    assert _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "rerun",
                "requested_target_node": "write_screenplay",
                "last_node": "integrate_acts",
            }
        },
        node_name="write_screenplay",
    )


def test_should_force_rewrite_for_last_node_rerun_without_explicit_target() -> None:
    assert _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "rerun",
                "requested_target_node": None,
                "last_node": "write_screenplay",
            }
        },
        node_name="write_screenplay",
    )


def test_should_not_force_rewrite_for_non_rerun_or_other_target() -> None:
    assert not _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "next",
                "requested_target_node": "write_screenplay",
                "last_node": "write_screenplay",
            }
        },
        node_name="write_screenplay",
    )
    assert not _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "rerun",
                "requested_target_node": "plan_acts",
                "last_node": "write_screenplay",
            }
        },
        node_name="write_screenplay",
    )
