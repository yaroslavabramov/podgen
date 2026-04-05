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
    assert "Adjacent acts should usually have different `internal_arc` shapes." in SHOWRUNNER_SYSTEM
    assert "All human-readable fields (`title`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`) MUST be in `output_language`" in SHOWRUNNER_SYSTEM
    assert "Branch coherence is a hint, not a rule." in SHOWRUNNER_SYSTEM
    assert "If two claims from different branches are near-paraphrases or materially describe the same mechanism" in SHOWRUNNER_SYSTEM
    assert "One historical event or institutional decision should usually have one main dramatic home." in SHOWRUNNER_SYSTEM
    assert "Listener novelty matters more than branch coverage." in SHOWRUNNER_SYSTEM
    assert "prefer a lean explanatory backbone over exhaustive inclusion" in SHOWRUNNER_SYSTEM
    assert "Before keeping a claim in an act, ask whether it earns a distinct scene move." in SHOWRUNNER_SYSTEM
    assert "Keep only the strongest 2-3 as fresh explanatory load" in SHOWRUNNER_SYSTEM
    assert "one dominant explanatory spine, not several equal mini-explanations" in SHOWRUNNER_SYSTEM
    assert "same operating rule, safeguard, or institutional constraint" in SHOWRUNNER_SYSTEM
    assert "Comparative material should stay selective." in SHOWRUNNER_SYSTEM
    assert "Vary `ending_turn` shapes across acts" in SHOWRUNNER_SYSTEM
    assert "total_claim_count" in SHOWRUNNER_SYSTEM
    assert "keep every act at or below" in SHOWRUNNER_SYSTEM
    assert "Distribute claim load roughly in proportion" in SHOWRUNNER_SYSTEM


def test_build_showrunner_prompt_payload_carries_output_language() -> None:
    payload = _build_showrunner_prompt_payload(
        duration_target_seconds=1800,
        output_language="ru",
        show_style="Conversational",
        rigor_policy="",
        claims=[],
        conflicts=[],
        disputed_claim_ids=[],
        duration_fill_layers=[],
    )

    assert payload["duration_target_seconds"] == 1800
    assert payload["output_language"] == "ru"
    assert payload["total_claim_count"] == 0
    assert payload["duration_fill_layers"] == []


def test_compute_max_claims_per_act_uses_ceiling() -> None:
    assert _compute_max_claims_per_act(52, 5) == 16
    assert _compute_max_claims_per_act(6, 3) == 6
    assert _compute_max_claims_per_act(0, 5) == 0


def test_rebalance_act_claim_distribution_caps_overloaded_act() -> None:
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

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [8, 1, 0]
    assert balanced["acts"][1]["claim_ids"] == [claim_ids[-1]]


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


def test_rebalance_act_claim_distribution_drops_overflow_when_adjacent_acts_are_full() -> None:
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

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [8, 8, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == act_one_claim_ids[:8]


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

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [8, 8, 0, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == assigned_claim_ids[:8]
    assert balanced["acts"][1]["claim_ids"] == assigned_claim_ids[8:16]
