import json

import pytest

from app.pipeline.phase5_planning.showrunner import (
    SHOWRUNNER_REVISION_SYSTEM,
    SHOWRUNNER_SYSTEM,
    _build_showrunner_prompt_payload,
    _build_showrunner_revision_payload,
    _compact_selection_act_view,
    _compute_max_claims_per_act,
    _normalize_showrunner_selection_issues,
    _normalize_plan,
    _rebalance_act_claim_distribution,
    _run_showrunner_selection_audit,
    _selected_claim_ids,
    _shorten_plan_for_selection_audit,
    _showrunner_selection_blockers,
    _validate_showrunner_selection_issue_ids,
)
from app.pipeline.short_ids import ShortIdMap


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


def test_showrunner_prompt_does_not_hardcode_three_acts() -> None:
    assert '"act_count": 3' not in SHOWRUNNER_SYSTEM
    assert "compress this to 3 acts" not in SHOWRUNNER_SYSTEM
    assert "do not default to 3 acts" in SHOWRUNNER_SYSTEM
    assert '"internal_arc": "short phrase describing how this act moves internally"' in SHOWRUNNER_SYSTEM
    assert "Adjacent acts should usually have different `internal_arc` shapes" in SHOWRUNNER_SYSTEM
    assert "Treat `story_role` as listener work, not topic label." in SHOWRUNNER_SYSTEM
    assert "`episode_goal` is the user's stated intent for the podcast" in SHOWRUNNER_SYSTEM
    assert "`root_question` is the best current statement of the episode-level question" in SHOWRUNNER_SYSTEM
    assert "Selection comes before act assignment." in SHOWRUNNER_SYSTEM
    assert "First choose the smallest coherent editorial backbone" in SHOWRUNNER_SYSTEM
    assert "Then decide how many distinct listener questions that selected backbone really supports" in SHOWRUNNER_SYSTEM
    assert "one dominant dramatic question or editorial burden" in SHOWRUNNER_SYSTEM
    assert "not from a fixed universal arc" in SHOWRUNNER_SYSTEM
    assert "The best opener might be anomaly, mechanism, crisis, history, conflict, or comparison." in SHOWRUNNER_SYSTEM
    assert "why is this subject strange, how does it work, and why does it matter?" not in SHOWRUNNER_SYSTEM
    assert "All human-readable fields (`title`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`) MUST be in `output_language`" in SHOWRUNNER_SYSTEM
    assert "A major shift in timeframe, abstraction level, or editorial function is evidence for a new act" in SHOWRUNNER_SYSTEM
    assert "Merge candidate act material only when it would produce substantially the same `opening_question`, `internal_arc`, and `ending_turn`." in SHOWRUNNER_SYSTEM
    assert "Role purity beats compactness." in SHOWRUNNER_SYSTEM
    assert "Before keeping a claim in an act, ask whether removing it would force a genuinely different beat." in SHOWRUNNER_SYSTEM
    assert "it is not fresh backbone" in SHOWRUNNER_SYSTEM
    assert "Allocate airtime, not just claims." in SHOWRUNNER_SYSTEM
    assert "one main anchor mechanism, event, example, or conflict" in SHOWRUNNER_SYSTEM
    assert "Prefer claims that earn airtime." in SHOWRUNNER_SYSTEM
    assert "change the listener's model instead of adding trivia" in SHOWRUNNER_SYSTEM
    assert "treat that as a sign of overload" in SHOWRUNNER_SYSTEM
    assert "Downrank claims that mainly provide taxonomy, decorative detail, broad comparator context, repeated thesis language" in SHOWRUNNER_SYSTEM
    assert "one dominant explanatory spine" in SHOWRUNNER_SYSTEM
    assert "real-world test" in SHOWRUNNER_SYSTEM
    assert "Ignore research-tree provenance when it does not help the editorial decision." in SHOWRUNNER_SYSTEM
    assert "Comparative material should stay selective; one strong outside comparator is usually enough for one beat." in SHOWRUNNER_SYSTEM
    assert "total_claim_count" in SHOWRUNNER_SYSTEM
    assert "Use `total_claim_count` as an overload smell, not a quota" in SHOWRUNNER_SYSTEM
    assert "The last act should include takeaways and a clear \"why this matters\" payoff." in SHOWRUNNER_SYSTEM
    assert "The last act should contain only claims that directly sharpen the final takeaway or reframe the episode-level question." in SHOWRUNNER_SYSTEM
    assert "Do not use the last act to park leftover operational facts that could be removed without changing the final takeaway." in SHOWRUNNER_SYSTEM

    top_level_rules = [line for line in SHOWRUNNER_SYSTEM.splitlines() if line.startswith("-")]
    assert len(top_level_rules) <= 20
    assert len(SHOWRUNNER_SYSTEM.split()) < 1150


def test_showrunner_revision_prompt_uses_revision_contract() -> None:
    assert "revising an existing podcast act plan" in SHOWRUNNER_REVISION_SYSTEM
    assert "`current_plan`: the current act plan to revise" in SHOWRUNNER_REVISION_SYSTEM
    assert "`revision_request.selection_issues`" in SHOWRUNNER_REVISION_SYSTEM
    assert "revise `current_plan`, not regenerate from scratch" in SHOWRUNNER_REVISION_SYSTEM
    assert "Treat `revision_request.selection_issues` as the highest-priority editorial diagnosis" in SHOWRUNNER_REVISION_SYSTEM
    assert "resolve tradeoffs across issues" in SHOWRUNNER_REVISION_SYSTEM
    assert "`minimal_spine_claim_ids`" in SHOWRUNNER_REVISION_SYSTEM
    assert "split or reshape acts before doing local claim swaps" in SHOWRUNNER_REVISION_SYSTEM
    assert "Preserve acts, timings, and claim placements that are not implicated by the issues." in SHOWRUNNER_REVISION_SYSTEM


def test_showrunner_selection_audit_prompt_covers_structural_failure_modes() -> None:
    from app.pipeline.phase5_planning.showrunner import SHOWRUNNER_SELECTION_AUDIT_SYSTEM

    assert "act_count_too_low" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "act_combines_incompatible_jobs" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "hook_overweights_decorative_detail" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "minimal_spine_missing" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "`minimal_spine_claim_ids`" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Resolve tradeoffs inside the audit" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "do not ask to remove a claim as redundant and then require it as missing support" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM


def test_build_showrunner_prompt_payload_carries_output_language() -> None:
    payload = _build_showrunner_prompt_payload(
        duration_target_seconds=1800,
        output_language="ru",
        show_style="Conversational",
        episode_goal="Explain why Hong Kong money works this way.",
        root_question="Why does Hong Kong let private banks issue money?",
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
    assert payload["episode_goal"] == "Explain why Hong Kong money works this way."
    assert payload["root_question"] == "Why does Hong Kong let private banks issue money?"
    assert "rigor_policy" not in payload
    assert payload["total_claim_count"] == 1
    assert "branch_ids" not in payload["claims"][0]
    assert "question_ids" not in payload["claims"][0]
    assert payload["duration_fill_layers"] == []


def test_build_showrunner_revision_payload_includes_current_plan_and_issues() -> None:
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])

    payload = _build_showrunner_revision_payload(
        base_payload={
            "duration_target_seconds": 1800,
            "output_language": "ru",
            "claims": [{"id": "c1", "statement": "test"}],
        },
        current_plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "purpose": "Explain it.",
                    "internal_arc": "rule -> consequence",
                    "opening_question": "How?",
                    "ending_turn": "Clear enough.",
                    "estimated_duration_seconds": 300,
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        revision_request={
            "message": "Fix the plan.",
            "selection_issues": [{"type": "weak_claim_selected", "severity": "medium"}],
        },
        sid=sid,
    )

    assert payload["current_plan"]["acts"][0]["claim_ids"] == ["c1"]
    assert payload["revision_request"]["message"] == "Fix the plan."


def test_normalize_showrunner_selection_issues_keeps_only_known_issue_types() -> None:
    normalized = _normalize_showrunner_selection_issues(
        [
            {
                "type": "weak_claim_selected",
                "severity": "medium",
                "act_number": 2,
                "selected_claim_ids": ["c40"],
                "omitted_claim_ids": ["c3"],
                "why_it_hurts": "Decorative detail displaced a stronger scene-bearing claim.",
                "fix_direction": "Drop the weaker detail.",
            },
            {
                "type": "minimal_spine_missing",
                "severity": "high",
                "minimal_spine_claim_ids": ["c10", "c11"],
                "why_it_hurts": "The act cannot explain its own mechanism.",
                "fix_direction": "Preserve the smallest viable spine.",
            },
            {
                "type": "invented_type",
                "severity": "high",
                "why_it_hurts": "Should be discarded.",
                "fix_direction": "Ignore me.",
            },
        ]
    )

    assert normalized == [
        {
            "type": "weak_claim_selected",
            "severity": "medium",
            "act_number": 2,
            "selected_claim_ids": ["c40"],
            "omitted_claim_ids": ["c3"],
            "why_it_hurts": "Decorative detail displaced a stronger scene-bearing claim.",
            "fix_direction": "Drop the weaker detail.",
        },
        {
            "type": "minimal_spine_missing",
            "severity": "high",
            "minimal_spine_claim_ids": ["c10", "c11"],
            "why_it_hurts": "The act cannot explain its own mechanism.",
            "fix_direction": "Preserve the smallest viable spine.",
        },
    ]


def test_showrunner_selection_blockers_only_returns_medium_or_high() -> None:
    blockers = _showrunner_selection_blockers(
        {
            "issues": [
                {"type": "weak_claim_selected", "severity": "low"},
                {"type": "goal_misalignment", "severity": "medium"},
                {"type": "act_overpacked_same_move", "severity": "high"},
            ]
        }
    )

    assert blockers == [
        {"type": "goal_misalignment", "severity": "medium"},
        {"type": "act_overpacked_same_move", "severity": "high"},
    ]


def test_validate_showrunner_selection_issue_ids_drops_invented_claim_ids() -> None:
    validated = _validate_showrunner_selection_issue_ids(
        [
            {
                "type": "strong_claim_omitted",
                "severity": "high",
                "selected_claim_ids": ["c999"],
                "omitted_claim_ids": ["c888"],
                "why_it_hurts": "Invented IDs.",
                "fix_direction": "Should not matter.",
            },
            {
                "type": "weak_claim_selected",
                "severity": "medium",
                "selected_claim_ids": ["c1", "c999"],
                "omitted_claim_ids": ["c2", "c888"],
                "why_it_hurts": "Valid selected claim has a weaker role.",
                "fix_direction": "Use valid IDs only.",
            },
        ],
        selected_ids={"c1"},
        omitted_ids={"c2"},
    )

    assert validated == [
        {
            "type": "weak_claim_selected",
            "severity": "medium",
            "selected_claim_ids": ["c1"],
            "omitted_claim_ids": ["c2"],
            "why_it_hurts": "Valid selected claim has a weaker role.",
            "fix_direction": "Use valid IDs only.",
        }
    ]


def test_validate_showrunner_selection_issue_ids_handles_structural_issue_types() -> None:
    validated = _validate_showrunner_selection_issue_ids(
        [
            {
                "type": "act_count_too_low",
                "severity": "high",
                "why_it_hurts": "Different listener jobs are compressed into one act.",
                "fix_direction": "Split the mechanism and crisis into separate acts.",
            },
            {
                "type": "minimal_spine_missing",
                "severity": "high",
                "minimal_spine_claim_ids": ["c1", "c2", "c999"],
                "omitted_claim_ids": ["c3", "c888"],
                "why_it_hurts": "The act lacks the minimal mechanism spine.",
                "fix_direction": "Build around the valid spine.",
            },
            {
                "type": "minimal_spine_missing",
                "severity": "medium",
                "minimal_spine_claim_ids": ["c999"],
                "omitted_claim_ids": ["c888"],
                "why_it_hurts": "Only invented IDs.",
                "fix_direction": "Should be dropped.",
            },
            {
                "type": "hook_overweights_decorative_detail",
                "severity": "medium",
                "selected_claim_ids": ["c2", "c999"],
                "why_it_hurts": "The setup spends too much time on decorative detail.",
                "fix_direction": "Downgrade the vivid detail to support.",
            },
            {
                "type": "hook_overweights_decorative_detail",
                "severity": "medium",
                "selected_claim_ids": ["c999"],
                "why_it_hurts": "No valid selected IDs.",
                "fix_direction": "Should be dropped.",
            },
        ],
        selected_ids={"c1", "c2"},
        omitted_ids={"c3"},
        all_ids={"c1", "c2", "c3"},
    )

    assert validated == [
        {
            "type": "act_count_too_low",
            "severity": "high",
            "why_it_hurts": "Different listener jobs are compressed into one act.",
            "fix_direction": "Split the mechanism and crisis into separate acts.",
        },
        {
            "type": "minimal_spine_missing",
            "severity": "high",
            "minimal_spine_claim_ids": ["c1", "c2"],
            "omitted_claim_ids": ["c3"],
            "why_it_hurts": "The act lacks the minimal mechanism spine.",
            "fix_direction": "Build around the valid spine.",
        },
        {
            "type": "hook_overweights_decorative_detail",
            "severity": "medium",
            "selected_claim_ids": ["c2"],
            "why_it_hurts": "The setup spends too much time on decorative detail.",
            "fix_direction": "Downgrade the vivid detail to support.",
        },
    ]


def test_selection_audit_uses_short_id_namespace_for_normalized_plan() -> None:
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    normalized_plan = {
        "act_count": 1,
        "acts": [
            {
                "act_number": 1,
                "story_role": "explain",
                "title": "Act 1",
                "purpose": "Explain the mechanism.",
                "internal_arc": "rule -> consequence",
                "opening_question": "How does it work?",
                "ending_turn": "The mechanism becomes concrete.",
                "estimated_duration_seconds": 300,
                "claim_ids": [claim_uuid],
            }
        ],
    }
    prompt_claim_lookup = {
        "c1": {
            "id": "c1",
            "statement": "Hong Kong banks issue notes against USD backing.",
            "type": "FACT",
            "verification": "verified",
            "intents": ["explain"],
            "has_illustrative": False,
            "direct_support_count": 2,
            "source_diversity": 2,
        }
    }

    audit_plan = _shorten_plan_for_selection_audit(normalized_plan, sid=sid)

    assert _selected_claim_ids(audit_plan) == ["c1"]
    assert _compact_selection_act_view(audit_plan, prompt_claim_lookup)[0]["claims"] == [
        {
            "id": "c1",
            "statement": "Hong Kong banks issue notes against USD backing.",
            "type": "FACT",
            "verification": "verified",
            "intents": ["explain"],
            "has_illustrative": False,
            "direct_support_count": 2,
            "source_diversity": 2,
        }
    ]


@pytest.mark.asyncio
async def test_selection_audit_failure_is_advisory_not_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bad_haiku(**_kwargs):
        return "not json"

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.call_haiku_structured",
        _bad_haiku,
    )
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    audit = await _run_showrunner_selection_audit(
        prompt_payload={
            "episode_goal": "Explain why Hong Kong money works this way.",
            "root_question": "Why private banknotes?",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Hong Kong banks issue notes against USD backing.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["explain"],
                    "has_illustrative": False,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                }
            ],
        },
        plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        sid=sid,
    )

    assert audit["passed"] is True
    assert audit["issues"] == []
    assert audit["audit_failed"] is True
    assert audit["error_type"] == "ValueError"
    assert "No valid JSON" in audit["error"]


@pytest.mark.asyncio
async def test_selection_audit_drops_parseable_issues_with_invented_claim_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _invented_issue(**_kwargs):
        return (
            '{"passed": false, "issues": ['
            '{"type": "strong_claim_omitted", "severity": "high", '
            '"selected_claim_ids": ["c999"], "omitted_claim_ids": ["c888"], '
            '"why_it_hurts": "Invented IDs.", "fix_direction": "Ignore."}'
            ']}'
        )

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.call_haiku_structured",
        _invented_issue,
    )
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    audit = await _run_showrunner_selection_audit(
        prompt_payload={
            "episode_goal": "Explain why Hong Kong money works this way.",
            "root_question": "Why private banknotes?",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Hong Kong banks issue notes against USD backing.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["explain"],
                    "has_illustrative": False,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
                {
                    "id": "c2",
                    "statement": "A stronger omitted claim.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["case_study"],
                    "has_illustrative": True,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
            ],
        },
        plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        sid=sid,
    )

    assert audit == {"passed": True, "issues": []}


@pytest.mark.asyncio
async def test_selection_audit_validates_before_limiting_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _invalid_then_valid_issue(**_kwargs):
        invalid_issues = [
            {
                "type": "strong_claim_omitted",
                "severity": "high",
                "omitted_claim_ids": [f"c999{idx}"],
                "why_it_hurts": "Invented ID.",
                "fix_direction": "Ignore.",
            }
            for idx in range(4)
        ]
        valid_issue = {
            "type": "goal_misalignment",
            "severity": "high",
            "why_it_hurts": "The selected act set no longer answers the episode goal.",
            "fix_direction": "Reshape the backbone around the stated goal.",
        }
        return json.dumps({"passed": False, "issues": invalid_issues + [valid_issue]})

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.call_haiku_structured",
        _invalid_then_valid_issue,
    )
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    audit = await _run_showrunner_selection_audit(
        prompt_payload={
            "episode_goal": "Explain why Hong Kong money works this way.",
            "root_question": "Why private banknotes?",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Hong Kong banks issue notes against USD backing.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["explain"],
                    "has_illustrative": False,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
                {
                    "id": "c2",
                    "statement": "A stronger omitted claim.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["case_study"],
                    "has_illustrative": True,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
            ],
        },
        plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        sid=sid,
    )

    assert audit == {
        "passed": False,
        "issues": [
            {
                "type": "goal_misalignment",
                "severity": "high",
                "why_it_hurts": "The selected act set no longer answers the episode goal.",
                "fix_direction": "Reshape the backbone around the stated goal.",
            }
        ],
    }


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
                "estimated_duration_seconds": 360,
                "claim_ids": claim_ids,
            },
            {
                "act_number": 2,
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
            {
                "act_number": 3,
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
                "estimated_duration_seconds": 360,
                "claim_ids": [claim_a, claim_b],
            },
            {
                "act_number": 2,
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
                "estimated_duration_seconds": 360,
                "claim_ids": act_one_claim_ids,
            },
            {
                "act_number": 2,
                "estimated_duration_seconds": 240,
                "claim_ids": act_two_claim_ids,
            },
            {
                "act_number": 3,
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
            {
                "act_number": 4,
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
                "estimated_duration_seconds": 360,
                "claim_ids": assigned_claim_ids,
            },
            {"act_number": 2, "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 3, "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 4, "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 5, "estimated_duration_seconds": 240, "claim_ids": []},
        ],
    }
    claim_lookup = {
        claim_id: {"id": claim_id, "branch_ids": ["b1"]}
        for claim_id in (assigned_claim_ids + extra_claim_ids)
    }

    balanced = _rebalance_act_claim_distribution(plan, claim_lookup=claim_lookup)

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [18, 0, 0, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == assigned_claim_ids
