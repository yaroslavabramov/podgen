from app.pipeline.phase5_planning.showrunner import (
    SHOWRUNNER_SYSTEM,
    _build_showrunner_prompt_payload,
    _compute_max_claims_per_act,
    _normalize_plan,
    _rebalance_act_claim_distribution,
)


def test_normalize_plan_preserves_story_fields() -> None:
    plan = _normalize_plan(
        {
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "setup",
                    "title": "How the money got weird",
                    "purpose": "Establish the strange starting point and why the listener should care.",
                    "internal_arc": "paradox -> failed fix -> institutional turn",
                    "opening_question": "Why would a city let private banks print money?",
                    "ending_turn": "The historical oddity becomes an institutional design puzzle.",
                    "focus_branch_ids": ["b1", "b2", "b1"],
                    "estimated_duration_seconds": 420,
                    "claim_ids": [],
                    "conflict_brief_ids": [],
                    "content_quotas": {},
                }
            ]
        },
        duration_target=1800,
    )

    act = plan["acts"][0]
    assert act["story_role"] == "setup"
    assert act["purpose"].startswith("Establish the strange starting point")
    assert act["internal_arc"] == "paradox -> failed fix -> institutional turn"
    assert act["opening_question"] == "Why would a city let private banks print money?"
    assert act["ending_turn"] == "The historical oddity becomes an institutional design puzzle."
    assert act["focus_branch_ids"] == ["b1", "b2"]


def test_showrunner_prompt_does_not_hardcode_three_acts() -> None:
    assert '"act_count": 3' not in SHOWRUNNER_SYSTEM
    assert "compress this to 3 acts" not in SHOWRUNNER_SYSTEM
    assert "do not default to 3 acts" in SHOWRUNNER_SYSTEM
    assert '"internal_arc": "short phrase describing how this act moves internally"' in SHOWRUNNER_SYSTEM
    assert "Adjacent acts should usually have different `internal_arc` shapes" in SHOWRUNNER_SYSTEM
    assert "Treat `story_role` as listener work, not topic label." in SHOWRUNNER_SYSTEM
    assert "Before assigning claims, first decide the act jobs." in SHOWRUNNER_SYSTEM
    assert "one dominant dramatic question or editorial burden" in SHOWRUNNER_SYSTEM
    assert "All human-readable fields (`title`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`) MUST be in `output_language`" in SHOWRUNNER_SYSTEM
    assert "A major shift in timeframe, abstraction level, or editorial function is evidence for a new act" in SHOWRUNNER_SYSTEM
    assert "Merge candidate act material only when it would produce substantially the same `opening_question`, `internal_arc`, and `ending_turn`." in SHOWRUNNER_SYSTEM
    assert "Role purity beats compactness." in SHOWRUNNER_SYSTEM
    assert "Listener novelty matters more than branch coverage; branch coherence is a hint, not a rule." in SHOWRUNNER_SYSTEM
    assert "Before keeping a claim in an act, ask whether removing it would force a genuinely different beat." in SHOWRUNNER_SYSTEM
    assert "it is not fresh backbone" in SHOWRUNNER_SYSTEM
    assert "one dominant explanatory spine" in SHOWRUNNER_SYSTEM
    assert "real-world test" in SHOWRUNNER_SYSTEM
    assert "`branch_ids` are provenance hints, not targets." in SHOWRUNNER_SYSTEM
    assert "Comparative material should stay selective; one strong outside comparator is usually enough for one beat." in SHOWRUNNER_SYSTEM
    assert "total_claim_count" in SHOWRUNNER_SYSTEM
    assert "keep every act at or below" in SHOWRUNNER_SYSTEM
    assert "The last act should include takeaways and a clear \"why this matters\" payoff." in SHOWRUNNER_SYSTEM
    assert "The last act should contain only claims that directly sharpen the final takeaway or reframe the episode-level question." in SHOWRUNNER_SYSTEM
    assert "Do not use the last act to park leftover operational facts that could be removed without changing the final takeaway." in SHOWRUNNER_SYSTEM

    top_level_rules = [line for line in SHOWRUNNER_SYSTEM.splitlines() if line.startswith("-")]
    assert len(top_level_rules) <= 20
    assert len(SHOWRUNNER_SYSTEM.split()) < 1150


def test_build_showrunner_prompt_payload_carries_output_language() -> None:
    payload = _build_showrunner_prompt_payload(
        duration_target_seconds=1800,
        output_language="ru",
        show_style="Conversational",
        rigor_policy="",
        claims=[
            {
                "id": "c1",
                "statement": "test",
                "branch_ids": ["b1"],
                "question_ids": ["b1.1"],
            }
        ],
        conflicts=[],
        disputed_claim_ids=[],
        duration_fill_layers=[],
    )

    assert payload["duration_target_seconds"] == 1800
    assert payload["output_language"] == "ru"
    assert payload["total_claim_count"] == 1
    assert payload["claims"][0]["branch_ids"] == ["b1"]
    assert "question_ids" not in payload["claims"][0]
    assert payload["duration_fill_layers"] == []


def test_compute_max_claims_per_act_uses_ceiling() -> None:
    assert _compute_max_claims_per_act(52, 5) == 16
    assert _compute_max_claims_per_act(6, 3) == 6
    assert _compute_max_claims_per_act(0, 5) == 0


def test_rebalance_act_claim_distribution_leaves_overloaded_act_unchanged() -> None:
    claim_ids = [str(i) * 8 + "-" + str(i) * 4 + "-" + str(i) * 4 + "-" + str(i) * 4 + "-" + str(i) * 12 for i in range(1, 10)]
    plan = {
        "act_count": 3,
        "acts": [
            {
                "act_number": 1,
                "focus_branch_ids": ["b1"],
                "estimated_duration_seconds": 360,
                "claim_ids": claim_ids,
            },
            {
                "act_number": 2,
                "focus_branch_ids": ["b2"],
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
            {
                "act_number": 3,
                "focus_branch_ids": ["b3"],
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
        ],
    }
    claim_lookup = {
        claim_id: {"id": claim_id, "branch_ids": ["b1" if idx < 8 else "b2"]}
        for idx, claim_id in enumerate(claim_ids)
    }

    balanced = _rebalance_act_claim_distribution(plan, claim_lookup=claim_lookup)

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [9, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == claim_ids


def test_rebalance_act_claim_distribution_dedupes_cross_act_duplicates() -> None:
    claim_a = "11111111-1111-1111-1111-111111111111"
    claim_b = "22222222-2222-2222-2222-222222222222"
    plan = {
        "act_count": 2,
        "acts": [
            {
                "act_number": 1,
                "focus_branch_ids": ["b1"],
                "estimated_duration_seconds": 360,
                "claim_ids": [claim_a, claim_b],
            },
            {
                "act_number": 2,
                "focus_branch_ids": ["b2"],
                "estimated_duration_seconds": 360,
                "claim_ids": [claim_b],
            },
        ],
    }

    balanced = _rebalance_act_claim_distribution(
        plan,
        claim_lookup={
            claim_a: {"id": claim_a, "branch_ids": ["b1"]},
            claim_b: {"id": claim_b, "branch_ids": ["b2"]},
        },
    )

    assert balanced["acts"][0]["claim_ids"] == [claim_a, claim_b]
    assert balanced["acts"][1]["claim_ids"] == []


def test_rebalance_act_claim_distribution_never_moves_or_drops_overflow_claims() -> None:
    act_one_claim_ids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
        "44444444-4444-4444-4444-444444444444",
        "55555555-5555-5555-5555-555555555555",
        "66666666-6666-6666-6666-666666666666",
        "77777777-7777-7777-7777-777777777777",
        "88888888-8888-8888-8888-888888888888",
        "99999999-9999-9999-9999-999999999999",
    ]
    act_two_claim_ids = [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa5",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa6",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa7",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa8",
    ]
    plan = {
        "act_count": 4,
        "acts": [
            {
                "act_number": 1,
                "focus_branch_ids": ["b1"],
                "estimated_duration_seconds": 360,
                "claim_ids": act_one_claim_ids,
            },
            {
                "act_number": 2,
                "focus_branch_ids": ["b2"],
                "estimated_duration_seconds": 240,
                "claim_ids": act_two_claim_ids,
            },
            {
                "act_number": 3,
                "focus_branch_ids": ["b3"],
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
            {
                "act_number": 4,
                "focus_branch_ids": ["b4"],
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
        ],
    }
    claim_lookup = {
        claim_id: {"id": claim_id, "branch_ids": ["b1"]}
        for claim_id in (act_one_claim_ids + act_two_claim_ids)
    }

    balanced = _rebalance_act_claim_distribution(plan, claim_lookup=claim_lookup)

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [9, 8, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == act_one_claim_ids
    assert balanced["acts"][1]["claim_ids"] == act_two_claim_ids


def test_rebalance_act_claim_distribution_uses_assigned_claim_count_for_ceiling() -> None:
    assigned_claim_ids = [
        f"{i:08d}-{i:04d}-{i:04d}-{i:04d}-{i:012d}"
        for i in range(1, 19)
    ]
    extra_claim_ids = [
        f"{i:08d}-{i:04d}-{i:04d}-{i:04d}-{i:012d}"
        for i in range(19, 97)
    ]
    plan = {
        "act_count": 5,
        "acts": [
            {
                "act_number": 1,
                "focus_branch_ids": ["b1"],
                "estimated_duration_seconds": 360,
                "claim_ids": assigned_claim_ids,
            },
            {"act_number": 2, "focus_branch_ids": ["b2"], "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 3, "focus_branch_ids": ["b3"], "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 4, "focus_branch_ids": ["b4"], "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 5, "focus_branch_ids": ["b5"], "estimated_duration_seconds": 240, "claim_ids": []},
        ],
    }
    claim_lookup = {
        claim_id: {"id": claim_id, "branch_ids": ["b1"]}
        for claim_id in (assigned_claim_ids + extra_claim_ids)
    }

    balanced = _rebalance_act_claim_distribution(plan, claim_lookup=claim_lookup)

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [18, 0, 0, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == assigned_claim_ids
