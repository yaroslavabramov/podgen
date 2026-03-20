from app.pipeline.phase5_planning.act_planner import (
    ACT_PLANNER_SYSTEM,
    _branch_focus_summary,
    _build_act_prompt_payload,
    _hydrate_scene_evidence_ids_from_claim_support,
    _extract_scene_strategy,
    _scene_count_guidance,
)


def test_build_act_prompt_payload_carries_story_contract_and_branch_focus() -> None:
    act = {
        "act_number": 2,
        "story_role": "explain",
        "title": "How the machine holds together",
        "purpose": "Explain the mechanism and remove the mystery from the system.",
        "internal_arc": "mystery -> mechanism -> constraint -> resolution",
        "opening_question": "What stops private note issuance from spinning out of control?",
        "ending_turn": "The listener sees that trust comes from the rules, not the logos on the notes.",
        "focus_branch_ids": ["b3", "b2"],
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
    )

    assert payload["output_language"] == "ru"
    assert payload["story_role"] == "explain"
    assert payload["purpose"].startswith("Explain the mechanism")
    assert payload["internal_arc"] == "mystery -> mechanism -> constraint -> resolution"
    assert payload["opening_question"].startswith("What stops private note issuance")
    assert payload["ending_turn"].startswith("The listener sees")
    assert payload["focus_branch_ids"] == ["b3", "b2"]
    assert payload["scene_count_guidance"] == {"min_scenes": 5, "target_scenes": 6, "max_scenes": 7}
    assert payload["branch_focus_summary"] == [
        {"branch_id": "b2", "claim_count": 2},
        {"branch_id": "b3", "claim_count": 2},
    ]
    assert payload["conflict_count"] == 0
    assert "evidence" not in payload
    assert "conflict_brief_ids" not in payload
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
            "branch_ids": ["b3"],
            "intents": ["explain"],
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
            "branch_ids": ["b3", "b2"],
            "intents": ["explain"],
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
            "branch_ids": ["b2"],
            "intents": ["compare"],
            "support_bundle": {
                "direct": [],
                "illustrative": [],
            },
        },
    ]


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
    assert "`internal_arc` says how the act should move internally" in ACT_PLANNER_SYSTEM
    assert "All human-readable output fields (`scene_strategy`, `description`, `transition_note`, `notes`) MUST be in `output_language`" in ACT_PLANNER_SYSTEM
    assert "first scene must explicitly open the act's `opening_question`" in ACT_PLANNER_SYSTEM
    assert "final scene must clearly deliver the act's `ending_turn`" in ACT_PLANNER_SYSTEM
    assert "Opening the act question does not require a `hook`." in ACT_PLANNER_SYSTEM
    assert "Landing the act turn does not require a `takeaway`." in ACT_PLANNER_SYSTEM
    assert "Do not default every act to `hook` at the start and `takeaway` at the end." in ACT_PLANNER_SYSTEM
    assert "Prefer compact beat cards, not mini-script paragraphs." in ACT_PLANNER_SYSTEM
    assert "Use `debate` only when the act has real conflict pressure" in ACT_PLANNER_SYSTEM
    assert "Use `scene_count_guidance` as the default size target" in ACT_PLANNER_SYSTEM
    assert "Act-to-act variety matters. Do not default every act to the same internal shape." in ACT_PLANNER_SYSTEM
    assert "Each act should contain at least one meaningful turn" in ACT_PLANNER_SYSTEM
    assert "Avoid the default podcast skeleton of hook -> explanation -> case_study -> takeaway" in ACT_PLANNER_SYSTEM
    assert "A `case_study` scene is optional, not mandatory." in ACT_PLANNER_SYSTEM
    assert "each claim includes `support_bundle.direct` and optional `support_bundle.illustrative`" in ACT_PLANNER_SYSTEM
    assert "attach one or more `evidence_ids` from that claim's bundled support whenever available" in ACT_PLANNER_SYSTEM


def test_extract_scene_strategy_reads_top_level_field() -> None:
    assert _extract_scene_strategy({"scene_strategy": "paradox -> failed fix -> pragmatic turn"}) == (
        "paradox -> failed fix -> pragmatic turn"
    )
    assert _extract_scene_strategy({"scene_strategy": "   "}) == ""
    assert _extract_scene_strategy({"beat_sheet": []}) == ""


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
