import json
import uuid
from types import SimpleNamespace

import pytest

from app.pipeline.phase5_planning.act_planner import (
    ACT_PLANNER_SYSTEM,
    _assign_scene_jobs,
    _build_act_prompt_payload,
    _collect_prior_acts_ending_types,
    _collect_prior_acts_primary_claims,
    _collect_beat_sheet_quality_issues,
    _hydrate_scene_evidence_ids_from_claim_support,
    _extract_scene_strategy,
    _format_structural_retry_detail,
    _has_structural_overlap_failures,
    _normalize_beat_sheet,
    _recover_scene_claim_ids,
    _recover_scene_evidence_ids,
    _run_llm_quality_checks,
    _scene_job,
    _scene_count_guidance,
    _split_scene_claim_roles,
    plan_act,
)
from app.pipeline.short_ids import ShortIdMap, restore_id_list


def test_build_act_prompt_payload_carries_story_contract_and_branch_focus() -> None:
    act = {
        "act_number": 2,
        "story_role": "explain",
        "title": "How the machine holds together",
        "purpose": "Explain the mechanism and remove the mystery from the system.",
        "internal_arc": "mystery -> mechanism -> constraint -> resolution",
        "opening_question": "What stops private note issuance from spinning out of control?",
        "ending_turn": "The listener sees that trust comes from the rules, not the logos on the notes.",
        "estimated_duration_seconds": 480,
        "content_quotas": {"verified_facts": 5},
    }
    act_claims = [
        {
            "id": "c1",
            "type": "fact",
            "verification": "verified",
            "branch_ids": ["b3"],
            "intents": ["explain"],
            "statement": "Claim 1",
            "has_illustrative": True,
            "illustrative_evidence_count": 1,
            "direct_support_count": 2,
            "source_diversity": 2,
        },
        {
            "id": "c2",
            "type": "fact",
            "verification": "observed",
            "branch_ids": ["b3", "b2"],
            "intents": ["explain"],
            "statement": "Claim 2",
            "has_illustrative": False,
            "illustrative_evidence_count": 0,
            "direct_support_count": 1,
            "source_diversity": 1,
        },
        {
            "id": "c3",
            "type": "opinion",
            "verification": "observed",
            "branch_ids": ["b2"],
            "intents": ["compare"],
            "statement": "Claim 3",
            "has_illustrative": False,
            "illustrative_evidence_count": 0,
            "direct_support_count": 0,
            "source_diversity": 1,
        },
    ]
    act_evidence = [
        {
            "id": "e1",
            "quote": "Direct support for claim 1",
            "asset_type": "mechanism",
            "source_title": "Primary memo",
            "source_reliability": "high",
            "authority_type": "primary",
            "is_temporary": False,
            "supports_fact": True,
            "illustrative": False,
            "narrative_score": 0.2,
            "direct_or_contextual": ["direct"],
            "supporting_claim_ids": ["c1"],
            "illustrative_claim_ids": [],
        },
        {
            "id": "e2",
            "quote": "Illustrative support for claim 1",
            "asset_type": "case",
            "source_title": "Field report",
            "source_reliability": "medium",
            "authority_type": "secondary",
            "is_temporary": False,
            "supports_fact": False,
            "illustrative": True,
            "narrative_score": 0.9,
            "direct_or_contextual": ["contextual"],
            "supporting_claim_ids": [],
            "illustrative_claim_ids": ["c1"],
        },
        {
            "id": "e3",
            "quote": "Direct support for claim 2",
            "asset_type": "timeline",
            "source_title": "Archive",
            "source_reliability": "high",
            "authority_type": "primary",
            "is_temporary": False,
            "supports_fact": True,
            "illustrative": False,
            "narrative_score": 0.3,
            "direct_or_contextual": ["direct"],
            "supporting_claim_ids": ["c2"],
            "illustrative_claim_ids": [],
        },
    ]

    payload = _build_act_prompt_payload(
        act=act,
        act_number=2,
        act_claims=act_claims,
        act_evidence=act_evidence,
        output_language="ru",
        prior_acts_primary_claims=[
            {
                "id": "p1",
                "statement": "Earlier act already established the reserve rule.",
                "act_number": 1,
            }
        ],
    )

    assert payload["output_language"] == "ru"
    assert payload["story_role"] == "explain"
    assert payload["purpose"].startswith("Explain the mechanism")
    assert payload["internal_arc"] == "mystery -> mechanism -> constraint -> resolution"
    assert payload["opening_question"].startswith("What stops private note issuance")
    assert payload["ending_turn"].startswith("The listener sees")
    assert payload["scene_count_guidance"] == {"min_scenes": 5, "target_scenes": 6, "max_scenes": 7}
    assert payload["conflict_count"] == 0
    assert payload["prior_acts_primary_claims"] == [
        {
            "id": "p1",
            "statement": "Earlier act already established the reserve rule.",
            "act_number": 1,
        }
    ]
    assert "evidence" not in payload
    assert "conflict_brief_ids" not in payload
    assert "focus_branch_ids" not in payload
    assert "branch_focus_summary" not in payload
    assert "content_quotas" not in payload
    assert payload["claims"] == [
        {
            "id": "c1",
            "type": "fact",
            "verification": "verified",
            "statement": "Claim 1",
            "has_illustrative": True,
            "illustrative_evidence_count": 1,
            "direct_support_count": 2,
            "source_diversity": 2,
            "support_bundle": {
                "direct": [
                    {
                        "id": "e1",
                        "quote": "Direct support for claim 1",
                        "asset_type": "mechanism",
                        "source_title": "Primary memo",
                        "authority_type": "primary",
                    }
                ],
                "illustrative": [
                    {
                        "id": "e2",
                        "quote": "Illustrative support for claim 1",
                        "asset_type": "case",
                        "source_title": "Field report",
                        "authority_type": "secondary",
                    }
                ],
            },
        },
        {
            "id": "c2",
            "type": "fact",
            "verification": "observed",
            "statement": "Claim 2",
            "has_illustrative": False,
            "illustrative_evidence_count": 0,
            "direct_support_count": 1,
            "source_diversity": 1,
            "support_bundle": {
                "direct": [
                    {
                        "id": "e3",
                        "quote": "Direct support for claim 2",
                        "asset_type": "timeline",
                        "source_title": "Archive",
                        "authority_type": "primary",
                    }
                ],
                "illustrative": [],
            },
        },
        {
            "id": "c3",
            "type": "opinion",
            "verification": "observed",
            "statement": "Claim 3",
            "has_illustrative": False,
            "illustrative_evidence_count": 0,
            "direct_support_count": 0,
            "source_diversity": 1,
            "support_bundle": {
                "direct": [],
                "illustrative": [],
            },
        },
    ]


def test_scene_count_guidance_tracks_duration_bands() -> None:
    assert _scene_count_guidance(180) == {"min_scenes": 3, "target_scenes": 4, "max_scenes": 5}
    assert _scene_count_guidance(300) == {"min_scenes": 4, "target_scenes": 5, "max_scenes": 6}
    assert _scene_count_guidance(420) == {"min_scenes": 5, "target_scenes": 6, "max_scenes": 7}
    assert _scene_count_guidance(600) == {"min_scenes": 6, "target_scenes": 7, "max_scenes": 8}


def test_act_planner_prompt_enforces_compact_beat_cards() -> None:
    assert "maximum 250 characters" in ACT_PLANNER_SYSTEM
    assert "State what CHANGES in this scene" in ACT_PLANNER_SYSTEM
    assert "If a description needs more than 3 sentences or more than 250 characters" in ACT_PLANNER_SYSTEM


@pytest.mark.asyncio
async def test_run_llm_quality_checks_sends_full_description_to_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_description = "Это длинная сцена. " * 35
    captured: dict[str, object] = {}
    claim_one = "11111111-1111-1111-1111-111111111111"
    claim_two = "22222222-2222-2222-2222-222222222222"
    claim_three = "33333333-3333-3333-3333-333333333333"

    async def _fake_grounding(scenes):
        captured["scenes"] = scenes
        return []

    async def _fake_overlap(_scenes):
        return []

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner._check_description_claim_grounding",
        _fake_grounding,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner._check_cross_scene_overlap",
        _fake_overlap,
    )

    issues = await _run_llm_quality_checks(
        [
            {
                "scene": 1,
                "type": "explanation",
                "description": long_description,
                "claim_ids": [claim_one],
            },
            {
                "scene": 2,
                "type": "case_study",
                "description": "Короткая сцена.",
                "primary_claim_ids": [claim_two],
                "callback_claim_ids": [claim_three],
            },
        ],
        {
            claim_one: "Claim one",
            claim_two: "Claim two",
            claim_three: "Claim three",
        },
    )

    assert issues == []
    assert captured["scenes"][0]["description"] == long_description.strip()
    assert len(captured["scenes"][0]["description"]) > 400
    assert captured["scenes"][0]["primary_claims"] == ["Claim one"]
    assert captured["scenes"][0]["callback_claims"] == []
    assert captured["scenes"][1]["primary_claims"] == ["Claim two"]
    assert captured["scenes"][1]["callback_claims"] == ["Claim three"]


def test_act_planner_prompt_defines_positive_contract_and_hard_constraints() -> None:
    assert '"scene_strategy": "short phrase describing the act\'s internal movement"' in ACT_PLANNER_SYSTEM
    assert "build the smallest sequence of scenes that gives the listener clear internal movement" in ACT_PLANNER_SYSTEM
    assert "Scenes are jobs, not slots." in ACT_PLANNER_SYSTEM
    assert '"primary_claim_ids": ["id"]' in ACT_PLANNER_SYSTEM
    assert '"callback_claim_ids": ["id"]' in ACT_PLANNER_SYSTEM
    assert "All human-readable output fields (`scene_strategy`, `description`, `transition_note`, `notes`) MUST be in `output_language`" in ACT_PLANNER_SYSTEM
    assert "the act should engage `opening_question` immediately" in ACT_PLANNER_SYSTEM
    assert "the final scene must clearly deliver `ending_turn`" in ACT_PLANNER_SYSTEM
    assert "Decide the minimum set of distinct scene moves needed to satisfy the act contract." in ACT_PLANNER_SYSTEM
    assert "`scene_count_guidance` is a soft range, not a quota." in ACT_PLANNER_SYSTEM
    assert "Assign each claim one main home scene using `primary_claim_ids`." in ACT_PLANNER_SYSTEM
    assert "`primary_claim_ids` should be unique within the act." in ACT_PLANNER_SYSTEM
    assert "Neighboring scenes must do different listener work." in ACT_PLANNER_SYSTEM
    assert "It is better to return fewer scenes than to keep a weak, repetitive, or purely connective scene." in ACT_PLANNER_SYSTEM
    assert "Do not restate the act's main reveal in the next scene as if it were new." in ACT_PLANNER_SYSTEM
    assert "`case_study` is optional." in ACT_PLANNER_SYSTEM
    assert "`debate` is optional." in ACT_PLANNER_SYSTEM
    assert "`takeaway` and `summary` scenes should land implication or reframing" in ACT_PLANNER_SYSTEM
    assert "Prefer compact beat cards, not mini-script paragraphs." in ACT_PLANNER_SYSTEM
    assert "A scene may draw a consequence from its claims and already-established act context." in ACT_PLANNER_SYSTEM
    assert "each claim includes `support_bundle.direct` and optional `support_bundle.illustrative`" in ACT_PLANNER_SYSTEM
    assert "attach one or more `evidence_ids` from that claim's bundled support whenever available" in ACT_PLANNER_SYSTEM
    assert "`primary_claim_ids`, `callback_claim_ids`, and `evidence_ids` are opaque labels" in ACT_PLANNER_SYSTEM
    assert "In historical acts, keep the scene order chronologically legible" in ACT_PLANNER_SYSTEM
    assert "When writing a callback-heavy or ending beat card, lead with the new move first" in ACT_PLANNER_SYSTEM
    assert "Use `cost` only when the scene is genuinely about what is lost" in ACT_PLANNER_SYSTEM
    assert "A `contrast` scene should usually have one dominant outside comparator." in ACT_PLANNER_SYSTEM
    assert "`prior_acts_primary_claims` lists material already established earlier." in ACT_PLANNER_SYSTEM
    assert "Use `scene_job` to declare the move each callback-heavy scene is making" in ACT_PLANNER_SYSTEM
    assert "`apply` scenes should begin from the concrete condition or operating case" in ACT_PLANNER_SYSTEM
    assert "`stress_test` scenes should begin from the pressure, failure mode, or attack" in ACT_PLANNER_SYSTEM
    assert "`synthesize` scenes should land the shift through their own claims" in ACT_PLANNER_SYSTEM
    assert "prior_acts_ending_types" in ACT_PLANNER_SYSTEM
    assert "Prefer a different closing scene type unless the material genuinely demands the same one." in ACT_PLANNER_SYSTEM


def test_collect_prior_acts_ending_types_returns_last_scene_types() -> None:
    acts = [
        {"act_number": 1, "beat_sheet": [{"type": "hook"}, {"type": "takeaway"}]},
        {"act_number": 2, "beat_sheet": [{"type": "explanation"}, {"type": "debate"}]},
        {"act_number": 3, "beat_sheet": [{"type": "case_study"}]},
    ]
    assert _collect_prior_acts_ending_types(acts, 0) == []
    assert _collect_prior_acts_ending_types(acts, 1) == ["takeaway"]
    assert _collect_prior_acts_ending_types(acts, 2) == ["takeaway", "debate"]
    assert _collect_prior_acts_ending_types(acts, 3) == ["takeaway", "debate", "case_study"]


def test_collect_prior_acts_ending_types_handles_empty_beat_sheets() -> None:
    acts = [
        {"act_number": 1, "beat_sheet": []},
        {"act_number": 2, "beat_sheet": [{"type": "hook"}]},
    ]
    assert _collect_prior_acts_ending_types(acts, 2) == ["hook"]


def test_collect_prior_acts_primary_claims_uses_planned_previous_acts_in_order() -> None:
    claim_a = "11111111-1111-1111-1111-111111111111"
    claim_b = "22222222-2222-2222-2222-222222222222"
    claim_c = "33333333-3333-3333-3333-333333333333"

    acts = [
        {
            "act_number": 1,
            "beat_sheet": [
                {"scene": 1, "primary_claim_ids": [claim_a]},
                {"scene": 2, "primary_claim_ids": [claim_b]},
            ],
        },
        {
            "act_number": 2,
            "beat_sheet": [
                {"scene": 1, "primary_claim_ids": [claim_c]},
            ],
        },
        {"act_number": 3, "beat_sheet": []},
    ]
    cache = {
        claim_a: {"statement": "Act one claim A"},
        claim_b: {"statement": "Act one claim B"},
        claim_c: {"statement": "Act two claim C"},
    }

    assert _collect_prior_acts_primary_claims(
        acts=acts,
        current_act_idx=3,
        claim_context_cache=cache,
    ) == [
        {"id": claim_a, "statement": "Act one claim A", "act_number": 1},
        {"id": claim_b, "statement": "Act one claim B", "act_number": 1},
        {"id": claim_c, "statement": "Act two claim C", "act_number": 2},
    ]


def test_extract_scene_strategy_reads_top_level_field() -> None:
    assert _extract_scene_strategy({"scene_strategy": "paradox -> failed fix -> pragmatic turn"}) == (
        "paradox -> failed fix -> pragmatic turn"
    )
    assert _extract_scene_strategy({"scene_strategy": "   "}) == ""
    assert _extract_scene_strategy({"beat_sheet": []}) == ""


def test_split_scene_claim_roles_assigns_first_normalized_use_to_primary() -> None:
    beat_sheet = [
        {"scene": 2, "type": "hook", "claim_ids": ["11111111-1111-1111-1111-111111111111"]},
        {
            "scene": 1,
            "type": "takeaway",
            "claim_ids": [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
        },
    ]

    _split_scene_claim_roles(beat_sheet)

    assert beat_sheet[0]["primary_claim_ids"] == []
    assert beat_sheet[0]["callback_claim_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert beat_sheet[0]["claim_ids"] == []
    assert beat_sheet[1]["primary_claim_ids"] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert beat_sheet[1]["callback_claim_ids"] == []
    assert beat_sheet[1]["claim_ids"] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]


def test_split_scene_claim_roles_preserves_explicit_llm_roles() -> None:
    claim_a = "11111111-1111-1111-1111-111111111111"
    claim_b = "22222222-2222-2222-2222-222222222222"
    beat_sheet = [
        {
            "scene": 1,
            "type": "hook",
            "claim_ids": [claim_a],
            "primary_claim_ids": [claim_a],
            "callback_claim_ids": [],
            "_claim_role_source": "explicit",
        },
        {
            "scene": 2,
            "type": "explanation",
            "claim_ids": [claim_b],
            "primary_claim_ids": [claim_b],
            "callback_claim_ids": [claim_a],
            "_claim_role_source": "explicit",
        },
    ]

    _split_scene_claim_roles(beat_sheet)

    assert beat_sheet[0]["primary_claim_ids"] == [claim_a]
    assert beat_sheet[0]["callback_claim_ids"] == []
    assert beat_sheet[0]["claim_ids"] == [claim_a]
    assert beat_sheet[1]["primary_claim_ids"] == [claim_b]
    assert beat_sheet[1]["callback_claim_ids"] == [claim_a]
    assert beat_sheet[1]["claim_ids"] == [claim_b]


def test_split_scene_claim_roles_demotes_duplicate_explicit_primary_to_callback() -> None:
    claim_a = "11111111-1111-1111-1111-111111111111"
    claim_b = "22222222-2222-2222-2222-222222222222"
    beat_sheet = [
        {
            "scene": 1,
            "type": "hook",
            "claim_ids": [claim_a],
            "primary_claim_ids": [claim_a],
            "callback_claim_ids": [],
            "_claim_role_source": "explicit",
        },
        {
            "scene": 2,
            "type": "explanation",
            "claim_ids": [claim_a, claim_b],
            "primary_claim_ids": [claim_a, claim_b],
            "callback_claim_ids": [],
            "_claim_role_source": "explicit",
        },
    ]

    _split_scene_claim_roles(beat_sheet)

    assert beat_sheet[0]["primary_claim_ids"] == [claim_a]
    assert beat_sheet[0]["callback_claim_ids"] == []
    assert beat_sheet[1]["primary_claim_ids"] == [claim_b]
    assert beat_sheet[1]["callback_claim_ids"] == [claim_a]
    assert beat_sheet[1]["claim_ids"] == [claim_b]


def test_split_scene_claim_roles_promotes_future_explicit_callback_to_first_primary_home() -> None:
    claim_a = "11111111-1111-1111-1111-111111111111"
    claim_b = "22222222-2222-2222-2222-222222222222"
    claim_c = "33333333-3333-3333-3333-333333333333"
    beat_sheet = [
        {
            "scene": 1,
            "type": "hook",
            "claim_ids": [claim_a],
            "primary_claim_ids": [claim_a],
            "callback_claim_ids": [claim_b],
            "_claim_role_source": "explicit",
        },
        {
            "scene": 2,
            "type": "explanation",
            "claim_ids": [claim_b, claim_c],
            "primary_claim_ids": [claim_b, claim_c],
            "callback_claim_ids": [],
            "_claim_role_source": "explicit",
        },
    ]

    _split_scene_claim_roles(beat_sheet)

    assert beat_sheet[0]["primary_claim_ids"] == [claim_a, claim_b]
    assert beat_sheet[0]["callback_claim_ids"] == []
    assert beat_sheet[0]["claim_ids"] == [claim_a, claim_b]
    assert beat_sheet[1]["primary_claim_ids"] == [claim_c]
    assert beat_sheet[1]["callback_claim_ids"] == [claim_b]
    assert beat_sheet[1]["claim_ids"] == [claim_c]


def test_assign_scene_jobs_defaults_callback_and_mixed_scenes() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"
    c2 = "22222222-2222-2222-2222-222222222222"
    c3 = "33333333-3333-3333-3333-333333333333"
    beat_sheet = [
        {"scene": 1, "type": "takeaway", "primary_claim_ids": [], "callback_claim_ids": [c1]},
        {"scene": 2, "type": "transition", "primary_claim_ids": [c2], "callback_claim_ids": [c1]},
        {"scene": 3, "type": "explanation", "primary_claim_ids": [c3], "callback_claim_ids": []},
    ]

    _assign_scene_jobs(beat_sheet)

    assert beat_sheet[0]["scene_job"] == "synthesize"
    assert beat_sheet[1]["scene_job"] == "bridge"
    assert "scene_job" not in beat_sheet[2]
    assert _scene_job(beat_sheet[0]) == "synthesize"


async def test_collect_beat_sheet_quality_issues_flags_structural_callback_only_explanation() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "primary_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "callback_claim_ids": [],
        },
        {
            "scene": 2,
            "type": "explanation",
            "primary_claim_ids": [],
            "callback_claim_ids": ["11111111-1111-1111-1111-111111111111"],
        },
    ]

    issues = await _collect_beat_sheet_quality_issues(beat_sheet)

    assert issues == [
        {
            "severity": "structural",
            "type": "callback_only_scene",
            "scene": 2,
            "scene_type": "explanation",
            "message": "Scene only references already-covered claims and introduces no primary claim.",
            "callback_claim_ids": ["11111111-1111-1111-1111-111111111111"],
        }
    ]
    assert _has_structural_overlap_failures(issues)


def test_format_structural_retry_detail_for_callback_only_scene_allows_shorter_act() -> None:
    claim_id = "11111111-1111-1111-1111-111111111111"
    detail = _format_structural_retry_detail(
        {
            "severity": "structural",
            "type": "callback_only_scene",
            "scene": 2,
            "scene_type": "case_study",
            "message": "Scene only references already-covered claims and introduces no primary claim.",
            "callback_claim_ids": [claim_id],
        },
        beat_sheet=[
            {
                "scene": 1,
                "type": "hook",
                "primary_claim_ids": [claim_id],
                "callback_claim_ids": [],
            },
            {
                "scene": 2,
                "type": "case_study",
                "scene_job": "apply",
                "primary_claim_ids": [],
                "callback_claim_ids": [claim_id],
            },
        ],
        claim_statements_lookup={
            claim_id: "Same concrete anomaly claim.",
        },
    )

    assert "Do NOT keep it just to preserve a template or to hit `scene_count_guidance`." in detail
    assert "delete it and return a shorter act" in detail
    assert "Scene 2 current move: apply" in detail
    assert "Same concrete anomaly claim." in detail


def test_format_structural_retry_detail_for_description_mismatch_includes_callback_context() -> None:
    primary_id = "11111111-1111-1111-1111-111111111111"
    callback_id = "22222222-2222-2222-2222-222222222222"
    detail = _format_structural_retry_detail(
        {
            "severity": "structural",
            "type": "description_claim_mismatch",
            "scene": 4,
            "message": "Description is not grounded in scene claims: too broad.",
        },
        beat_sheet=[
            {
                "scene": 4,
                "type": "debate",
                "primary_claim_ids": [primary_id],
                "callback_claim_ids": [callback_id],
            }
        ],
        claim_statements_lookup={
            primary_id: "Shops refuse the 1000 HKD note.",
            callback_id: "Multiple versions of the same note circulate at once.",
        },
    )

    assert "Scene's actual claims and callback context:" in detail
    assert "Shops refuse the 1000 HKD note." in detail
    assert "Multiple versions of the same note circulate at once." in detail


async def test_collect_beat_sheet_quality_issues_keeps_callback_only_takeaway_advisory() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "primary_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "callback_claim_ids": [],
        },
        {
            "scene": 2,
            "type": "takeaway",
            "primary_claim_ids": [],
            "callback_claim_ids": ["11111111-1111-1111-1111-111111111111"],
        },
    ]

    issues = await _collect_beat_sheet_quality_issues(beat_sheet)

    assert issues == [
        {
            "severity": "advisory",
            "type": "callback_only_ending",
            "scene": 2,
            "scene_type": "takeaway",
            "message": "Ending scene is callback-only; ensure it lands a new consequence or reframing instead of reteaching mechanism.",
            "callback_claim_ids": ["11111111-1111-1111-1111-111111111111"],
        }
    ]
    assert not _has_structural_overlap_failures(issues)


async def test_collect_beat_sheet_quality_issues_flags_oversized_callback_only_ending() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "primary_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "callback_claim_ids": [],
            "description": "Explain the mechanism.",
        },
        {
            "scene": 2,
            "type": "takeaway",
            "primary_claim_ids": [],
            "callback_claim_ids": [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            ],
            "scene_job": "apply",
            "description": "x" * 321,
        },
    ]

    issues = await _collect_beat_sheet_quality_issues(beat_sheet)

    assert any(issue["type"] == "callback_only_ending_too_many_claims" for issue in issues)
    assert any(
        issue["type"] == "callback_only_ending_too_long" and issue["severity"] == "advisory"
        for issue in issues
    )
    assert any(issue["type"] == "callback_only_ending_bad_scene_job" for issue in issues)
    assert _has_structural_overlap_failures(issues)


@pytest.mark.asyncio
async def test_collect_beat_sheet_quality_issues_flags_overlong_beat_card_structural() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "primary_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "callback_claim_ids": [],
            "description": "x" * 251,
        },
        {
            "scene": 2,
            "type": "case_study",
            "primary_claim_ids": ["22222222-2222-2222-2222-222222222222"],
            "callback_claim_ids": [],
            "description": "Short beat.",
        },
    ]

    issues = await _collect_beat_sheet_quality_issues(beat_sheet)

    assert issues == [
        {
            "severity": "structural",
            "type": "beat_card_description_too_long",
            "scene": 1,
            "scene_type": "explanation",
            "message": "Scene description exceeds the beat-card budget and may drift into mini-draft narration.",
            "description_chars": 251,
        }
    ]
    assert _has_structural_overlap_failures(issues)


@pytest.mark.asyncio
async def test_collect_beat_sheet_quality_issues_keeps_callback_only_ending_advisory_but_blocks_long_beat_card() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "primary_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "callback_claim_ids": [],
            "description": "Explain the mechanism.",
        },
        {
            "scene": 2,
            "type": "takeaway",
            "primary_claim_ids": [],
            "callback_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "scene_job": "reframe",
            "description": "x" * 321,
        },
    ]

    issues = await _collect_beat_sheet_quality_issues(beat_sheet)

    assert issues == [
        {
            "severity": "structural",
            "type": "beat_card_description_too_long",
            "scene": 2,
            "scene_type": "takeaway",
            "message": "Scene description exceeds the beat-card budget and may drift into mini-draft narration.",
            "description_chars": 321,
        },
        {
            "severity": "advisory",
            "type": "callback_only_ending",
            "scene": 2,
            "scene_type": "takeaway",
            "message": "Ending scene is callback-only; ensure it lands a new consequence or reframing instead of reteaching mechanism.",
            "callback_claim_ids": ["11111111-1111-1111-1111-111111111111"],
        },
        {
            "severity": "advisory",
            "type": "callback_only_ending_too_long",
            "scene": 2,
            "scene_type": "takeaway",
            "message": "Callback-only ending description is too long and likely to become a recap block.",
            "description_chars": 321,
            "scene_job": "reframe",
        },
    ]
    assert _has_structural_overlap_failures(issues)


def test_hydrate_scene_evidence_ids_from_claim_support_prefers_scene_type_order() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "hook",
            "claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "evidence_ids": [],
        },
        {
            "scene": 2,
            "type": "explanation",
            "claim_ids": ["22222222-2222-2222-2222-222222222222"],
            "evidence_ids": [],
        },
    ]
    claim_support_lookup = {
        "11111111-1111-1111-1111-111111111111": {
            "direct": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            "illustrative": ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
        },
        "22222222-2222-2222-2222-222222222222": {
            "direct": ["cccccccc-cccc-cccc-cccc-cccccccccccc"],
            "illustrative": ["dddddddd-dddd-dddd-dddd-dddddddddddd"],
        },
    }

    hydrated = _hydrate_scene_evidence_ids_from_claim_support(beat_sheet, claim_support_lookup)

    assert beat_sheet[0]["evidence_ids"] == [
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ]
    assert beat_sheet[1]["evidence_ids"] == [
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "dddddddd-dddd-dddd-dddd-dddddddddddd",
    ]
    assert hydrated == [
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "dddddddd-dddd-dddd-dddd-dddddddddddd",
    ]


def test_hydrate_scene_evidence_ids_from_claim_support_tops_up_partial_multi_claim_scene() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "claim_ids": [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
            "evidence_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
        }
    ]
    claim_support_lookup = {
        "11111111-1111-1111-1111-111111111111": {
            "direct": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            "illustrative": [],
        },
        "22222222-2222-2222-2222-222222222222": {
            "direct": ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
            "illustrative": [],
        },
    }

    hydrated = _hydrate_scene_evidence_ids_from_claim_support(beat_sheet, claim_support_lookup)

    assert beat_sheet[0]["evidence_ids"] == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    assert hydrated == ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]


def test_hydrate_scene_evidence_ids_from_claim_support_uses_callback_claims_for_callback_only_ending() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "primary_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "callback_claim_ids": [],
            "evidence_ids": [],
        },
        {
            "scene": 2,
            "type": "takeaway",
            "primary_claim_ids": [],
            "callback_claim_ids": ["11111111-1111-1111-1111-111111111111"],
            "evidence_ids": [],
        },
    ]
    claim_support_lookup = {
        "11111111-1111-1111-1111-111111111111": {
            "direct": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            "illustrative": [],
        }
    }

    hydrated = _hydrate_scene_evidence_ids_from_claim_support(beat_sheet, claim_support_lookup)

    assert beat_sheet[0]["evidence_ids"] == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
    assert beat_sheet[1]["evidence_ids"] == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
    assert hydrated == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]


def test_hydrate_scene_evidence_ids_from_claim_support_tops_up_mixed_scene_with_callback_support() -> None:
    primary_claim = "11111111-1111-1111-1111-111111111111"
    callback_claim = "22222222-2222-2222-2222-222222222222"
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "primary_claim_ids": [primary_claim],
            "callback_claim_ids": [callback_claim],
            "evidence_ids": [],
        }
    ]
    claim_support_lookup = {
        primary_claim: {
            "direct": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            "illustrative": [],
        },
        callback_claim: {
            "direct": ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
            "illustrative": [],
        },
    }

    hydrated = _hydrate_scene_evidence_ids_from_claim_support(beat_sheet, claim_support_lookup)

    assert beat_sheet[0]["evidence_ids"] == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    assert hydrated == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]


class _FakeResult:
    def __init__(self, scalar) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeActPlannerDB:
    def __init__(self, manifest) -> None:
        self.manifest = manifest

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _statement):
        return _FakeResult(self.manifest)

    async def refresh(self, _obj) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_plan_act_raises_after_overlap_gate_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = str(uuid.uuid4())
    manifest = SimpleNamespace(
        id=uuid.uuid4(),
        acts_json=[
            {
                "act_number": 1,
                "title": "Mechanism",
                "claim_ids": [claim_id],
                "estimated_duration_seconds": 300,
            }
        ],
        research_tree_json={"branches": []},
    )
    db = _FakeActPlannerDB(manifest)
    dumps = []

    async def _fake_load_planning_context(**_kwargs):
        return (
            {
                claim_id: {
                    "id": claim_id,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b1"],
                    "intents": ["explain"],
                    "statement": "Mechanism claim",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 0,
                    "source_diversity": 1,
                }
            },
            {},
        )

    async def _fake_call_sonnet_structured(**_kwargs):
        # Extract the short claim ID from the prompt payload
        prompt_data = json.loads(_kwargs.get("prompt", "{}"))
        short_cid = prompt_data.get("claims", [{}])[0].get("id", claim_id)
        return json.dumps(
            {
                "scene_strategy": "explain -> callback",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "type": "explanation",
                        "description": "Explain the mechanism.",
                        "claim_ids": [short_cid],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                    {
                        "scene": 2,
                        "type": "explanation",
                        "description": "Explain the same mechanism again.",
                        "claim_ids": [short_cid],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.pipeline.phase5_planning.act_planner.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.load_planning_context",
        _fake_load_planning_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.publish_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner._dump_act_planner_debug",
        lambda **kwargs: dumps.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="Act planner failed after 2 attempts for act 1"):
        await plan_act(
            {
                "generation_run_id": str(uuid.uuid4()),
                "manifest_id": str(manifest.id),
                "language": "ru",
            }
        )

    assert len(dumps) == 2
    assert dumps[0]["status"] == "started"
    assert dumps[0]["beat_sheet"] is None
    assert dumps[1]["status"] == "quality_gate_failed"
    assert dumps[1]["beat_sheet"] is not None
    assert dumps[1]["beat_sheet"][1]["primary_claim_ids"] == []
    assert dumps[1]["beat_sheet"][1]["callback_claim_ids"] == [claim_id]


@pytest.mark.asyncio
async def test_plan_act_writes_started_dump_before_llm_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    claim_id = str(uuid.uuid4())
    manifest = SimpleNamespace(
        id=uuid.uuid4(),
        acts_json=[
            {
                "act_number": 1,
                "story_role": "explain",
                "title": "Act 1",
                "purpose": "Explain",
                "internal_arc": "open -> close",
                "opening_question": "Why?",
                "ending_turn": "Because.",
                "claim_ids": [claim_id],
                "estimated_duration_seconds": 240,
                "content_quotas": {},
            }
        ],
        research_tree_json={"branches": []},
    )
    db = _FakeActPlannerDB(manifest)
    dumps = []

    async def _fake_load_planning_context(**_kwargs):
        return (
            {
                claim_id: {
                    "id": claim_id,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b1"],
                    "intents": ["explain"],
                    "statement": "Mechanism claim",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 0,
                    "source_diversity": 1,
                }
            },
            {},
        )

    async def _fake_call_sonnet_structured(**_kwargs):
        raise TimeoutError("Anthropic Sonnet structured request timed out after 300s")

    monkeypatch.setattr("app.pipeline.phase5_planning.act_planner.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.load_planning_context",
        _fake_load_planning_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.publish_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner._dump_act_planner_debug",
        lambda **kwargs: dumps.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="Act planner failed after 2 attempts for act 1"):
        await plan_act(
            {
                "generation_run_id": str(uuid.uuid4()),
                "manifest_id": str(manifest.id),
                "language": "ru",
            }
        )

    assert [dump["status"] for dump in dumps] == ["started", "llm_or_normalize_failed"]
    assert dumps[1]["attempts"]
    assert dumps[1]["attempts"][-1]["error"].startswith("TimeoutError:")


@pytest.mark.asyncio
async def test_plan_act_keeps_quality_retry_budget_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    claim_id_one = str(uuid.uuid4())
    claim_id_two = str(uuid.uuid4())
    manifest = SimpleNamespace(
        id=uuid.uuid4(),
        acts_json=[
            {
                "act_number": 1,
                "story_role": "explain",
                "title": "Act 1",
                "purpose": "Explain",
                "internal_arc": "open -> close",
                "opening_question": "Why?",
                "ending_turn": "Because.",
                "claim_ids": [claim_id_one, claim_id_two],
                "estimated_duration_seconds": 240,
                "content_quotas": {},
            }
        ],
        research_tree_json={"branches": []},
    )
    db = _FakeActPlannerDB(manifest)
    dumps = []
    prompts = []
    responses = iter(
        [
            TimeoutError("Anthropic Sonnet structured request timed out after 300s"),
            {
                "scene_strategy": "repeat the same mechanism",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "type": "explanation",
                        "description": "Explain the mechanism.",
                        "claim_ids": [claim_id_one],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                    {
                        "scene": 2,
                        "type": "explanation",
                        "description": "Explain the same mechanism again.",
                        "claim_ids": [claim_id_two],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                ],
            },
            {
                "scene_strategy": "explain -> land",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "type": "explanation",
                        "description": "Explain the mechanism once.",
                        "claim_ids": [claim_id_one],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                    {
                        "scene": 2,
                        "type": "takeaway",
                        "scene_job": "reframe",
                        "description": "Land the implication without reteaching the mechanism.",
                        "claim_ids": [claim_id_two],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                ],
            },
        ]
    )

    async def _fake_load_planning_context(**_kwargs):
        return (
            {
                claim_id_one: {
                    "id": claim_id_one,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b1"],
                    "intents": ["explain"],
                    "statement": "Mechanism claim A",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 0,
                    "source_diversity": 1,
                },
                claim_id_two: {
                    "id": claim_id_two,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b1"],
                    "intents": ["explain"],
                    "statement": "Mechanism claim B",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 0,
                    "source_diversity": 1,
                }
            },
            {},
        )

    async def _fake_call_sonnet_structured(**kwargs):
        prompts.append(json.loads(kwargs["prompt"]))
        next_response = next(responses)
        if isinstance(next_response, Exception):
            raise next_response
        return json.dumps(next_response, ensure_ascii=False)

    monkeypatch.setattr("app.pipeline.phase5_planning.act_planner.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.load_planning_context",
        _fake_load_planning_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.publish_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner._dump_act_planner_debug",
        lambda **kwargs: dumps.append(kwargs),
    )

    result = await plan_act(
        {
            "generation_run_id": str(uuid.uuid4()),
            "manifest_id": str(manifest.id),
            "project_id": str(uuid.uuid4()),
            "language": "ru",
        }
    )

    assert result["manifest_id"] == str(manifest.id)
    assert len(prompts) == 3
    assert "previous_error" not in prompts[1]
    assert prompts[2]["previous_error"].startswith(
        "ValueError: beat_sheet failed structural beat validation after claim-role normalization"
    )
    assert "Do NOT solve this by paraphrasing the same mechanism in new words." in prompts[2]["previous_error"]
    assert "merge it into the stronger scene" in prompts[2]["previous_error"]
    assert "demote it to a brief callback" in prompts[2]["previous_error"]
    assert "Scene 1 current move: none" in prompts[2]["previous_error"]
    assert "Scene 2 current move: none" in prompts[2]["previous_error"]
    assert "Mechanism claim A" in prompts[2]["previous_error"]
    assert "Mechanism claim B" in prompts[2]["previous_error"]
    assert dumps[-1]["status"] == "ok"
    assert [attempt["status"] for attempt in dumps[-1]["attempts"]] == [
        "llm_or_normalize_failed",
        "quality_gate_failed",
        "ok",
    ]


@pytest.mark.asyncio
async def test_plan_act_final_dump_status_tracks_last_failure_type(monkeypatch: pytest.MonkeyPatch) -> None:
    claim_id = str(uuid.uuid4())
    manifest = SimpleNamespace(
        id=uuid.uuid4(),
        acts_json=[
            {
                "act_number": 1,
                "story_role": "explain",
                "title": "Act 1",
                "purpose": "Explain",
                "internal_arc": "open -> close",
                "opening_question": "Why?",
                "ending_turn": "Because.",
                "claim_ids": [claim_id],
                "estimated_duration_seconds": 240,
                "content_quotas": {},
            }
        ],
        research_tree_json={"branches": []},
    )
    db = _FakeActPlannerDB(manifest)
    dumps = []
    responses = iter(
        [
            {
                "scene_strategy": "repeat the same mechanism",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "type": "explanation",
                        "description": "Explain the mechanism.",
                        "claim_ids": [claim_id],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                    {
                        "scene": 2,
                        "type": "explanation",
                        "description": "Explain the same mechanism again.",
                        "claim_ids": [claim_id],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                ],
            },
            TimeoutError("Anthropic Sonnet structured request timed out after 300s"),
            TimeoutError("Anthropic Sonnet structured request timed out after 300s"),
        ]
    )

    async def _fake_load_planning_context(**_kwargs):
        return (
            {
                claim_id: {
                    "id": claim_id,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b1"],
                    "intents": ["explain"],
                    "statement": "Mechanism claim",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 0,
                    "source_diversity": 1,
                }
            },
            {},
        )

    async def _fake_call_sonnet_structured(**_kwargs):
        next_response = next(responses)
        if isinstance(next_response, Exception):
            raise next_response
        return json.dumps(next_response, ensure_ascii=False)

    monkeypatch.setattr("app.pipeline.phase5_planning.act_planner.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.load_planning_context",
        _fake_load_planning_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.publish_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner._dump_act_planner_debug",
        lambda **kwargs: dumps.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="Act planner failed after 3 attempts for act 1"):
        await plan_act(
            {
                "generation_run_id": str(uuid.uuid4()),
                "manifest_id": str(manifest.id),
                "project_id": str(uuid.uuid4()),
                "language": "ru",
            }
        )

    assert [attempt["status"] for attempt in dumps[-1]["attempts"]] == [
        "quality_gate_failed",
        "llm_or_normalize_failed",
        "llm_or_normalize_failed",
    ]
    assert dumps[-1]["status"] == "llm_or_normalize_failed"
    assert dumps[-1]["beat_sheet"] is not None


@pytest.mark.asyncio
async def test_plan_act_short_ids_do_not_break_support_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    manifest = SimpleNamespace(
        id=uuid.uuid4(),
        acts_json=[
            {
                "act_number": 1,
                "story_role": "explain",
                "title": "Act 1",
                "purpose": "Explain",
                "internal_arc": "open -> close",
                "opening_question": "Why?",
                "ending_turn": "Because.",
                "claim_ids": [claim_id],
                "estimated_duration_seconds": 180,
                "content_quotas": {},
            }
        ],
        research_tree_json={"branches": []},
    )
    db = _FakeActPlannerDB(manifest)

    async def _fake_load_planning_context(**_kwargs):
        return (
            {
                claim_id: {
                    "id": claim_id,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b1"],
                    "intents": ["explain"],
                    "statement": "Mechanism claim",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 1,
                    "source_diversity": 1,
                    "support_bundle": {
                        "direct": [{"id": evidence_id, "quote": "Direct quote"}],
                        "illustrative": [],
                    },
                }
            },
            {
                evidence_id: {
                    "id": evidence_id,
                    "quote": "Direct quote",
                    "asset_type": "mechanism",
                    "source_title": "Archive source",
                    "source_reliability": "high",
                    "authority_type": "primary",
                    "is_temporary": False,
                    "supports_fact": True,
                    "illustrative": False,
                    "narrative_score": 0.0,
                    "direct_or_contextual": ["direct"],
                    "supporting_claim_ids": [claim_id],
                    "illustrative_claim_ids": [],
                }
            },
        )

    async def _fake_call_sonnet_structured(**kwargs):
        prompt_data = json.loads(kwargs["prompt"])
        short_cid = prompt_data["claims"][0]["id"]
        short_eid = prompt_data["claims"][0]["support_bundle"]["direct"][0]["id"]
        return json.dumps(
            {
                "scene_strategy": "explain once",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "type": "explanation",
                        "description": "Explain the mechanism once.",
                        "claim_ids": [short_cid],
                        "evidence_ids": [short_eid],
                        "estimated_duration_seconds": 120,
                    }
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.pipeline.phase5_planning.act_planner.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.load_planning_context",
        _fake_load_planning_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.publish_event",
        lambda *_args, **_kwargs: None,
    )

    await plan_act(
        {
            "generation_run_id": str(uuid.uuid4()),
            "manifest_id": str(manifest.id),
            "project_id": str(uuid.uuid4()),
            "language": "ru",
        }
    )

    beat_sheet = manifest.acts_json[0]["beat_sheet"]
    assert beat_sheet[0]["claim_ids"] == [claim_id]
    assert beat_sheet[0]["evidence_ids"] == [evidence_id]


@pytest.mark.asyncio
async def test_plan_act_short_ids_still_feed_llm_quality_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id_1 = str(uuid.uuid4())
    claim_id_2 = str(uuid.uuid4())
    manifest = SimpleNamespace(
        id=uuid.uuid4(),
        acts_json=[
            {
                "act_number": 1,
                "story_role": "explain",
                "title": "Act 1",
                "purpose": "Explain",
                "internal_arc": "open -> close",
                "opening_question": "Why?",
                "ending_turn": "Because.",
                "claim_ids": [claim_id_1, claim_id_2],
                "estimated_duration_seconds": 240,
                "content_quotas": {},
            }
        ],
        research_tree_json={"branches": []},
    )
    db = _FakeActPlannerDB(manifest)
    dumps = []

    async def _fake_load_planning_context(**_kwargs):
        return (
            {
                claim_id_1: {
                    "id": claim_id_1,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b1"],
                    "intents": ["explain"],
                    "statement": "Claim one",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 0,
                    "source_diversity": 1,
                },
                claim_id_2: {
                    "id": claim_id_2,
                    "type": "fact",
                    "verification": "verified",
                    "branch_ids": ["b2"],
                    "intents": ["explain"],
                    "statement": "Claim two",
                    "has_illustrative": False,
                    "illustrative_evidence_count": 0,
                    "direct_support_count": 0,
                    "source_diversity": 1,
                },
            },
            {},
        )

    async def _fake_call_sonnet_structured(**kwargs):
        prompt_data = json.loads(kwargs["prompt"])
        short_ids = [claim["id"] for claim in prompt_data["claims"]]
        return json.dumps(
            {
                "scene_strategy": "repeat disconnected thesis",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "type": "explanation",
                        "description": "Description not grounded in the actual claim.",
                        "claim_ids": [short_ids[0]],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                    {
                        "scene": 2,
                        "type": "explanation",
                        "description": "Another disconnected description.",
                        "claim_ids": [short_ids[1]],
                        "evidence_ids": [],
                        "estimated_duration_seconds": 120,
                    },
                ],
            },
            ensure_ascii=False,
        )

    async def _fake_call_haiku_structured(**kwargs):
        prompt_data = json.loads(kwargs["prompt"])
        if "disconnected_scenes" in prompt_data.get("return_format", {}):
            return json.dumps(
                {
                    "disconnected_scenes": [
                        {"scene": scene["scene"], "reason": "description not grounded"}
                        for scene in prompt_data["scenes"]
                    ]
                },
                ensure_ascii=False,
            )
        return json.dumps({"overlapping_pairs": []}, ensure_ascii=False)

    monkeypatch.setattr("app.pipeline.phase5_planning.act_planner.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.load_planning_context",
        _fake_load_planning_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.call_haiku_structured",
        _fake_call_haiku_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.publish_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner._dump_act_planner_debug",
        lambda **kwargs: dumps.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="Act planner failed after 2 attempts for act 1"):
        await plan_act(
            {
                "generation_run_id": str(uuid.uuid4()),
                "manifest_id": str(manifest.id),
                "project_id": str(uuid.uuid4()),
                "language": "ru",
            }
        )

    assert dumps[-1]["status"] == "quality_gate_failed"
    assert any(issue["type"] == "description_claim_mismatch" for issue in dumps[-1]["quality_issues"])


def test_build_act_prompt_payload_keeps_claim_support_beyond_old_flat_evidence_cap() -> None:
    act = {
        "act_number": 4,
        "story_role": "payoff",
        "title": "Why it survived",
        "purpose": "Show the system under pressure.",
        "internal_arc": "pressure -> reveal -> payoff",
        "opening_question": "Why did it survive stress?",
        "ending_turn": "The listener sees that the strange design is the point.",
        "focus_branch_ids": ["b5"],
        "estimated_duration_seconds": 300,
        "content_quotas": {},
    }
    claim_id = "c-last"
    act_claims = [
        {
            "id": claim_id,
            "type": "fact",
            "verification": "observed",
            "statement": "Late support claim",
            "has_illustrative": False,
            "illustrative_evidence_count": 0,
            "direct_support_count": 1,
            "source_diversity": 1,
            "branch_ids": ["b5"],
            "intents": ["explain"],
        }
    ]
    act_evidence = [
        {
            "id": f"e-{i}",
            "quote": f"Noise quote {i}",
            "asset_type": "mechanism",
            "source_title": "Noise source",
            "source_reliability": "medium",
            "authority_type": "secondary",
            "is_temporary": False,
            "supports_fact": False,
            "illustrative": False,
            "narrative_score": 0.1,
            "direct_or_contextual": ["contextual"],
            "supporting_claim_ids": [],
            "illustrative_claim_ids": [],
        }
        for i in range(20)
    ] + [
        {
            "id": "e-target",
            "quote": "Late direct support that used to fall past evidence[:20].",
            "asset_type": "timeline",
            "source_title": "Archive source",
            "source_reliability": "high",
            "authority_type": "primary",
            "is_temporary": False,
            "supports_fact": True,
            "illustrative": False,
            "narrative_score": 0.3,
            "direct_or_contextual": ["direct"],
            "supporting_claim_ids": [claim_id],
            "illustrative_claim_ids": [],
        }
    ]

    payload = _build_act_prompt_payload(
        act=act,
        act_number=4,
        act_claims=act_claims,
        act_evidence=act_evidence,
        output_language="ru",
        prior_acts_primary_claims=[],
    )

    assert payload["claims"][0]["support_bundle"]["direct"] == [
        {
            "id": "e-target",
            "quote": "Late direct support that used to fall past evidence[:20].",
            "asset_type": "timeline",
            "source_title": "Archive source",
            "authority_type": "primary",
        }
    ]


# ── Claim-id recovery tests ────────────────────────────────────


def test_recover_scene_claim_ids_passes_valid_through():
    cid = "11111111-1111-1111-1111-111111111111"
    allowed = {cid}
    result = _recover_scene_claim_ids([cid], allowed, scene_number=1)
    assert result == [cid]


def test_recover_scene_claim_ids_drops_invalid_uuid():
    """Invalid UUID format is dropped, not fuzzy-matched."""
    correct = "36e57f2b-4107-4365-9b3c-bade11b91674"
    typo = "36e57f2b-4107-4365-9b3c-bae11b91674"  # invalid UUID (35 chars)
    allowed = {correct}
    result = _recover_scene_claim_ids([typo], allowed, scene_number=1)
    assert result == []


def test_recover_scene_claim_ids_drops_unknown():
    allowed = {"11111111-1111-1111-1111-111111111111"}
    result = _recover_scene_claim_ids(["not-a-uuid-at-all"], allowed, scene_number=1)
    assert result == []


def test_recover_scene_claim_ids_drops_valid_uuid_not_in_allowed():
    """Valid UUID not in allowed set is dropped."""
    allowed = {"11111111-1111-1111-1111-111111111111"}
    other = "22222222-2222-2222-2222-222222222222"
    result = _recover_scene_claim_ids([other], allowed, scene_number=1)
    assert result == []


def test_recover_scene_evidence_ids_passes_valid_and_short_through() -> None:
    eid = "11111111-1111-1111-1111-111111111111"
    result = _recover_scene_evidence_ids([eid, "e1"], {eid, "e1"}, scene_number=1)
    assert result == [eid, "e1"]


def test_recover_scene_evidence_ids_drops_invalid_and_unknown() -> None:
    allowed = {"11111111-1111-1111-1111-111111111111"}
    other = "22222222-2222-2222-2222-222222222222"
    result = _recover_scene_evidence_ids(["not-an-id", other], allowed, scene_number=1)
    assert result == []


def test_normalize_beat_sheet_drops_invalid_claim_id():
    correct = "36e57f2b-4107-4365-9b3c-bade11b91674"
    typo = "36e57f2b-4107-4365-9b3c-bae11b91674"
    scenes = [
        {
            "scene": 1,
            "type": "explanation",
            "description": "Test scene",
            "claim_ids": [typo, correct],
            "evidence_ids": [],
            "estimated_duration_seconds": 60,
        }
    ]
    result = _normalize_beat_sheet(
        scenes,
        allowed_claim_ids={correct},
        allowed_evidence_ids=set(),
        default_duration=60,
    )
    assert len(result) == 1
    assert result[0]["claim_ids"] == [correct]


def test_normalize_beat_sheet_accepts_explicit_primary_and_callback_claim_ids() -> None:
    primary = "36e57f2b-4107-4365-9b3c-bade11b91674"
    callback = "46e57f2b-4107-4365-9b3c-bade11b91675"
    scenes = [
        {
            "scene": 1,
            "type": "explanation",
            "description": "Test scene",
            "primary_claim_ids": [primary],
            "callback_claim_ids": [callback, primary],
            "evidence_ids": [],
            "estimated_duration_seconds": 60,
        }
    ]

    result = _normalize_beat_sheet(
        scenes,
        allowed_claim_ids={primary, callback},
        allowed_evidence_ids=set(),
        default_duration=60,
    )

    assert len(result) == 1
    assert result[0]["primary_claim_ids"] == [primary]
    assert result[0]["callback_claim_ids"] == [callback]
    assert result[0]["claim_ids"] == [primary]


def test_explicit_claim_roles_survive_short_id_restore_runtime_path() -> None:
    primary = "36e57f2b-4107-4365-9b3c-bade11b91674"
    callback = "46e57f2b-4107-4365-9b3c-bade11b91675"
    claim_sid = ShortIdMap({primary, callback}, prefix="c")
    scenes = [
        {
            "scene": 1,
            "type": "explanation",
            "description": "Test scene",
            "primary_claim_ids": [claim_sid.shorten(primary)],
            "callback_claim_ids": [],
            "evidence_ids": [],
            "estimated_duration_seconds": 60,
        },
        {
            "scene": 2,
            "type": "explanation",
            "description": "Follow-up scene",
            "primary_claim_ids": [claim_sid.shorten(callback)],
            "callback_claim_ids": [claim_sid.shorten(primary)],
            "evidence_ids": [],
            "estimated_duration_seconds": 60,
        }
    ]

    normalized = _normalize_beat_sheet(
        scenes,
        allowed_claim_ids={primary, callback} | claim_sid.short_ids,
        allowed_evidence_ids=set(),
        default_duration=60,
    )
    for scene in normalized:
        scene["claim_ids"] = restore_id_list(scene.get("claim_ids"), claim_sid)
        scene["primary_claim_ids"] = restore_id_list(scene.get("primary_claim_ids"), claim_sid)
        scene["callback_claim_ids"] = restore_id_list(scene.get("callback_claim_ids"), claim_sid)

    result = _split_scene_claim_roles(normalized)

    assert result[0]["primary_claim_ids"] == [primary]
    assert result[0]["callback_claim_ids"] == []
    assert result[0]["claim_ids"] == [primary]
    assert result[1]["primary_claim_ids"] == [callback]
    assert result[1]["callback_claim_ids"] == [primary]
    assert result[1]["claim_ids"] == [callback]
