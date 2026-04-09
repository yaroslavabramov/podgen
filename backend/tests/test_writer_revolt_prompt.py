from types import SimpleNamespace

import pytest

from app.pipeline.phase6_writing.writer_revolt import (
    ACT_AUDIT_SYSTEM,
    EVAL_SYSTEM,
    MULTI_SCENE_POLISH_SYSTEM,
    POLISH_SYSTEM,
    _attempt_dialogue_polish,
    _attempt_multi_scene_polish,
    _dialogue_act_monotony_signals,
    _augment_act_dialogue_reports_with_llm,
    _build_act_dialogue_reports,
    _build_act_dialogue_audit_prompt_payload,
    _build_boundary_context,
    _build_dialogue_audit_prompt_payload,
    _build_dialogue_rhythm_metrics,
    _build_dialogue_polish_prompt_payload,
    _build_episode_seam_audit_prompt_payload,
    _build_multi_scene_polish_candidates,
    _build_multi_scene_polish_prompt_payload,
    _build_targeted_seam_audit_prompt_payload,
    _finalize_dialogue_seam_reports,
    _dialogue_polish_resolves_targets,
    _merge_act_dialogue_report,
    _normalize_dialogue_issues,
    _normalize_act_dialogue_audit,
    _normalize_episode_seam_audit,
    _normalize_revolt_counts_by_act,
    _opener_changed_materially,
    _prioritize_dialogue_interventions,
    _requires_material_opener_change,
    _row_should_polish_dialogue,
    _seam_reports_by_target,
    _should_polish_dialogue,
    _should_request_scene_rewrite,
    evaluate_and_revolt,
)
from app.pipeline.step_control import StepExecutionStopped


def test_writer_revolt_prompt_tracks_boundary_context_and_structured_repair_scopes() -> None:
    assert "boundary_restatement" in EVAL_SYSTEM
    assert "undefined_actor" in EVAL_SYSTEM
    assert "unexplained_term" in EVAL_SYSTEM
    assert "namedrop_without_grounding" in EVAL_SYSTEM
    assert "causal_gap" in EVAL_SYSTEM
    assert "summary_instead_of_scene" in EVAL_SYSTEM
    assert '"severity": "low|medium|high"' in EVAL_SYSTEM
    assert '"scope": "scene_polish|scene_rewrite|act_warning"' in EVAL_SYSTEM
    assert "return a compact machine-facing report" in EVAL_SYSTEM
    assert "return at most 3 issues" in EVAL_SYSTEM
    assert "Write `description` and `suggestion` in English" in EVAL_SYSTEM
    assert "closely repeating or paraphrasing the previous scene's punchline" in EVAL_SYSTEM
    assert "topic bridge or thesis-to-example progression is not echo by itself" in EVAL_SYSTEM
    assert "undefined actors" in EVAL_SYSTEM
    assert "use `scene_metadata` when present as the intended clarity contract" in EVAL_SYSTEM
    assert "The payload includes `language`." in EVAL_SYSTEM
    assert "Judge the scene as spoken dialogue in that language" in EVAL_SYSTEM
    assert "Replace the current single-scene score gate" not in EVAL_SYSTEM
    assert "Treat this as a targeted intervention, not a generic rewrite." in POLISH_SYSTEM
    assert "The payload includes `language`; keep the scene natural for that language and spoken register." in POLISH_SYSTEM
    assert "The payload may include `scene_metadata`" in POLISH_SYSTEM
    assert "If the original scene contains a foreign-language quote or source snippet" in POLISH_SYSTEM
    assert "Keep the `speaker` field canonical: output only `A` or `B`" in POLISH_SYSTEM
    assert "`claim_ids` are opaque labels" in POLISH_SYSTEM
    assert "Avoid translated-from-English phrasing or register drift." in POLISH_SYSTEM
    assert "Use `intervention_targets`, `intervention_mode`, and `success_criteria` as the primary contract." in POLISH_SYSTEM
    assert "The payload may include an `act_repetition_report`" in POLISH_SYSTEM
    assert "The payload may include a `seam_finding`" in POLISH_SYSTEM
    assert "If the target problem is comprehension, prioritize listener clarity over clever phrasing" in POLISH_SYSTEM
    assert "If you split, merge, or otherwise change line alignment" in POLISH_SYSTEM
    assert "If the boundary context already landed a punchline, do not reopen by paraphrasing it." in POLISH_SYSTEM
    assert "If this scene opens a new act, continue the same conversation" in POLISH_SYSTEM
    assert "Do not default to `B asks -> A confirms -> A explains`." in POLISH_SYSTEM
    assert "When fixing repetitive agreement shapes, prefer removing explicit confirmation markers" in POLISH_SYSTEM
    assert "`intervention_mode` tells you which repair type matters most" in POLISH_SYSTEM
    assert "clarity repair" in POLISH_SYSTEM
    assert "`success_criteria` lists what must become true" in POLISH_SYSTEM
    assert "targeted cluster intervention" in MULTI_SCENE_POLISH_SYSTEM
    assert "Do not let all scenes open with the same skeptical-question reset." in MULTI_SCENE_POLISH_SYSTEM
    assert "Do not move claim material from one scene into another" in MULTI_SCENE_POLISH_SYSTEM
    assert "act-level podcast dialogue auditor" in ACT_AUDIT_SYSTEM
    assert '"priority_scene_numbers": [2, 4]' in ACT_AUDIT_SYSTEM
    assert "Focus on act-level distribution problems" in ACT_AUDIT_SYSTEM
    assert "full scene dialogue for the act" in ACT_AUDIT_SYSTEM


def test_build_boundary_context_uses_last_two_lines_of_previous_scene() -> None:
    previous = SimpleNamespace(
        act_number=2,
        scene_number=4,
        dialogue_json=[
            {"speaker": "A", "text": "First line."},
            {"speaker": "B", "text": "Second line."},
            {"speaker": "A", "text": "Third line."},
        ],
    )

    assert _build_boundary_context(previous) == {
        "act_number": 2,
        "scene_number": 4,
        "last_lines": ["Second line.", "Third line."],
        "last_line": "Third line.",
    }


def test_build_dialogue_audit_prompt_payload_includes_boundary_context() -> None:
    segment = SimpleNamespace(
        act_number=3,
        scene_number=2,
        dialogue_json=[{"speaker": "A", "text": "Current scene."}],
    )
    boundary_context = {"last_line": "Previous punchline."}

    assert _build_dialogue_audit_prompt_payload(
        segment=segment,
        language="ru",
        boundary_context=boundary_context,
        scene_job="stress_test",
        scene_metadata={
            "scene_type": "case_study",
            "description": "Show the pressure point.",
            "listener_state_before": "Listener knows the rule.",
            "listener_state_after": "Listener sees the cost.",
            "must_make_clear": ["The pressure is concrete."],
            "must_not_assume": ["Listener knows the institutional name already."],
            "new_terms_to_introduce": ["corridor"],
            "required_bridge_from_previous_scene": "Move from mechanism to cost.",
        },
    ) == {
        "act_number": 3,
        "scene_number": 2,
        "language": "ru",
        "scene_job": "stress_test",
        "boundary_context": {"last_line": "Previous punchline."},
        "scene_metadata": {
            "scene_type": "case_study",
            "description": "Show the pressure point.",
            "listener_state_before": "Listener knows the rule.",
            "listener_state_after": "Listener sees the cost.",
            "must_make_clear": ["The pressure is concrete."],
            "must_not_assume": ["Listener knows the institutional name already."],
            "new_terms_to_introduce": ["corridor"],
            "required_bridge_from_previous_scene": "Move from mechanism to cost.",
        },
        "dialogue": [{"speaker": "A", "text": "Current scene."}],
    }


def test_build_dialogue_polish_prompt_payload_includes_scene_job() -> None:
    segment = SimpleNamespace(
        act_number=4,
        scene_number=6,
        dialogue_json=[{"speaker": "B", "text": "Current ending."}],
    )

    assert _build_dialogue_polish_prompt_payload(
        segment=segment,
        language="ru",
        scene_job="cost",
        scene_metadata={
            "scene_type": "debate",
            "description": "Land the price of intervention.",
            "listener_state_before": "Listener knows the defense worked.",
            "listener_state_after": "Listener knows what it cost.",
            "must_make_clear": ["The intervention had a price."],
            "must_not_assume": [],
            "new_terms_to_introduce": [],
            "required_bridge_from_previous_scene": "Carry success into cost.",
        },
        boundary_context={"last_line": "Previous line."},
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        act_repetition_report={"dominant_opening_speaker": "A"},
    ) == {
        "act_number": 4,
        "scene_number": 6,
        "language": "ru",
        "scene_job": "cost",
        "scene_metadata": {
            "scene_type": "debate",
            "description": "Land the price of intervention.",
            "listener_state_before": "Listener knows the defense worked.",
            "listener_state_after": "Listener knows what it cost.",
            "must_make_clear": ["The intervention had a price."],
            "must_not_assume": [],
            "new_terms_to_introduce": [],
            "required_bridge_from_previous_scene": "Carry success into cost.",
        },
        "boundary_context": {"last_line": "Previous line."},
        "act_repetition_report": {"dominant_opening_speaker": "A"},
        "seam_finding": None,
        "intervention_targets": [],
        "intervention_mode": "general_polish",
        "success_criteria": [
            "Preserve the scene job, claim coverage, chronology, and attribution.",
            "Keep the scene in the payload language and preserve delivery metadata compatibility.",
            "Keep the scene anchored in its declared job: `cost`.",
        ],
        "issues": [{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        "dialogue": [{"speaker": "B", "text": "Current ending."}],
    }


def test_normalize_dialogue_issues_defaults_invalid_values_to_repairable_medium() -> None:
    issues = _normalize_dialogue_issues(
        [
            {
                "type": "scene_opening_echo",
                "severity": "unknown",
                "scope": "weird",
                "description": "Opens by repeating the prior punchline.",
                "suggestion": "Advance instead of restating.",
            }
        ]
    )

    assert issues == [
        {
            "type": "scene_opening_echo",
            "severity": "medium",
            "scope": "scene_polish",
            "description": "Opens by repeating the prior punchline.",
            "suggestion": "Advance instead of restating.",
        }
    ]


def test_normalize_dialogue_issues_dedupes_and_caps_output() -> None:
    issues = _normalize_dialogue_issues(
        [
            {"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish", "description": "a", "suggestion": "b"},
            {"type": "scene_opening_echo", "severity": "high", "scope": "scene_rewrite", "description": "dup", "suggestion": "dup"},
            {"type": "rigid_turn_taking", "severity": "low", "scope": "act_warning", "description": "c", "suggestion": "d"},
            {"type": "teacherly_exposition", "severity": "low", "scope": "act_warning", "description": "e", "suggestion": "f"},
            {"type": "dry_facts", "severity": "medium", "scope": "scene_polish", "description": "g", "suggestion": "h"},
        ]
    )

    assert [issue["type"] for issue in issues] == [
        "scene_opening_echo",
        "dry_facts",
        "rigid_turn_taking",
    ]
    assert issues[0]["severity"] == "high"
    assert issues[0]["scope"] == "scene_rewrite"


def test_normalize_dialogue_issues_keeps_late_high_priority_issue_after_cap() -> None:
    issues = _normalize_dialogue_issues(
        [
            {"type": "scene_opening_echo", "severity": "low", "scope": "act_warning", "description": "a", "suggestion": "a"},
            {"type": "rigid_turn_taking", "severity": "low", "scope": "act_warning", "description": "b", "suggestion": "b"},
            {"type": "teacherly_exposition", "severity": "low", "scope": "act_warning", "description": "c", "suggestion": "c"},
            {"type": "persona_mismatch", "severity": "high", "scope": "scene_rewrite", "description": "late", "suggestion": "late"},
        ]
    )

    assert [issue["type"] for issue in issues] == [
        "persona_mismatch",
        "scene_opening_echo",
        "rigid_turn_taking",
    ]
    assert issues[0]["scope"] == "scene_rewrite"


def test_dialogue_issue_scopes_drive_polish_vs_rewrite() -> None:
    assert _should_polish_dialogue(
        [{"type": "rigid_turn_taking", "severity": "medium", "scope": "scene_polish"}]
    )
    assert not _should_polish_dialogue(
        [{"type": "dry_facts", "severity": "low", "scope": "act_warning"}]
    )
    assert _should_request_scene_rewrite(
        [{"type": "teacherly_exposition", "severity": "high", "scope": "scene_rewrite"}]
    )


def test_normalize_revolt_counts_by_act_coerces_keys_and_counts() -> None:
    assert _normalize_revolt_counts_by_act({"1": "2", 2: 3, "oops": "bad"}) == {
        "1": 2,
        "2": 3,
    }


def test_requires_opener_change_and_detects_material_change() -> None:
    issues = [{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}]
    assert _requires_material_opener_change(issues)
    assert not _opener_changed_materially(
        [{"speaker": "A", "text": "Exactly, that was the price."}],
        [{"speaker": "A", "text": "Exactly, that was the price!"}],
    )
    assert _opener_changed_materially(
        [{"speaker": "A", "text": "Exactly, that was the price."}],
        [{"speaker": "B", "text": "But that price landed somewhere specific."}],
    )


def test_act_dialogue_reports_prioritize_echo_and_late_act_scenes_locally() -> None:
    act1_scene2 = SimpleNamespace(
        act_number=1,
        scene_number=2,
        dialogue_json=[
            {"speaker": "A", "text": "Exactly, that was the price."},
            {"speaker": "B", "text": "So what changed next?"},
        ],
    )
    act2_scene6 = SimpleNamespace(
        act_number=2,
        scene_number=6,
        dialogue_json=[
            {"speaker": "A", "text": "Exactly, that was the price."},
            {"speaker": "A", "text": "And now the consequence arrives."},
        ],
    )
    act2_scene3 = SimpleNamespace(
        act_number=2,
        scene_number=3,
        dialogue_json=[
            {"speaker": "A", "text": "Here is a dry list."},
            {"speaker": "B", "text": "Another item from the list."},
        ],
    )
    rows = [
        {
            "segment": act1_scene2,
            "issues": [{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        },
        {
            "segment": act2_scene6,
            "issues": [{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        },
        {
            "segment": act2_scene3,
            "issues": [{"type": "teacherly_exposition", "severity": "medium", "scope": "scene_polish"}],
        },
    ]

    act_reports = _build_act_dialogue_reports(rows)
    prioritized = _prioritize_dialogue_interventions(rows, act_reports)

    assert act_reports["2"]["priority_scene_numbers"][0] == 6
    assert [row["segment"].scene_number for row in prioritized if row["segment"].act_number == 2] == [6, 3]


def test_row_with_act_level_target_needs_priority_selection_without_local_medium_issue() -> None:
    row = {
        "issues": [{"type": "scene_opening_echo", "severity": "low", "scope": "act_warning"}],
        "intervention_targets": ["act:repeated_confirmation_first_opening"],
        "llm_priority_selected": False,
    }

    assert not _should_polish_dialogue(row["issues"])
    assert _row_should_polish_dialogue(row) is False

    row["llm_priority_selected"] = True
    assert _row_should_polish_dialogue(row) is True


def test_llm_priority_scene_is_not_dropped_without_distribution_findings() -> None:
    row = {
        "segment": SimpleNamespace(
            act_number=2,
            scene_number=3,
            dialogue_json=[
                {"speaker": "A", "text": "A fresh opener."},
                {"speaker": "B", "text": "And a second line."},
            ],
        ),
        "issues": [],
        "intervention_targets": [],
        "priority_score": 0,
    }

    act_reports = {
        "2": {
            "act_number": 2,
            "scene_count": 4,
            "llm_priority_scene_numbers": [3],
            "distribution_findings": [],
        }
    }

    from app.pipeline.phase6_writing.writer_revolt import _refresh_dialogue_priorities

    _refresh_dialogue_priorities([row], act_reports)

    assert row["llm_priority_selected"] is True
    assert row["priority_score"] > 0
    assert _row_should_polish_dialogue(row) is True
    prioritized = _prioritize_dialogue_interventions([row], act_reports)
    assert [item["segment"].scene_number for item in prioritized] == [3]


def test_build_act_dialogue_audit_prompt_payload_includes_full_act_scene_cards() -> None:
    row = {
        "segment": SimpleNamespace(
            act_number=2,
            scene_number=4,
            dialogue_json=[
                {"speaker": "A", "text": "Exactly, that was the price."},
                {"speaker": "B", "text": "But who paid it?"},
                {"speaker": "A", "text": "The market paid immediately."},
            ],
        ),
        "scene_job": "stress_test",
        "issues": [
            {"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"},
            {"type": "repetitive_agreement_shape", "severity": "medium", "scope": "scene_polish"},
        ],
        "priority_score": 42,
    }
    payload = _build_act_dialogue_audit_prompt_payload(
        act_report={
            "act_number": 2,
            "scene_count": 6,
            "opening_speaker_counts": {"A": 5},
            "closing_speaker_counts": {"A": 4},
            "issue_counts": {"scene_opening_echo": 4},
            "repetition_families": [{"family": "scene_opening_echo", "scene_numbers": [2, 4], "count": 2}],
        },
        audit_rows=[row],
        language="ru",
    )

    assert payload == {
        "language": "ru",
        "act_number": 2,
        "structured_context": {
            "scene_count": 6,
            "opening_speaker_counts": {"A": 5},
            "closing_speaker_counts": {"A": 4},
            "issue_counts": {"scene_opening_echo": 4},
            "turn_pattern_counts": None,
            "rhythm_metrics": None,
            "heuristic_repetition_families": [{"family": "scene_opening_echo", "scene_numbers": [2, 4], "count": 2}],
        },
        "scenes": [
            {
                "scene_number": 4,
                "scene_job": "stress_test",
                "opening_speaker": "A",
                "closing_speaker": "A",
                "turn_pattern": "ABA",
                "line_count": 3,
                "issue_types": ["scene_opening_echo", "repetitive_agreement_shape"],
                "priority_score": 42,
                "dialogue": [
                    {"speaker": "A", "text": "Exactly, that was the price."},
                    {"speaker": "B", "text": "But who paid it?"},
                    {"speaker": "A", "text": "The market paid immediately."},
                ],
            }
        ],
    }


def test_normalize_and_merge_act_dialogue_audit() -> None:
    normalized = _normalize_act_dialogue_audit(
        {
            "priority_scene_numbers": ["2", 4, "oops"],
            "distribution_findings": [
                {
                    "family": "repeated_confirmation_first_opening",
                    "severity": "high",
                    "scene_numbers": [2, "4"],
                    "description": "Too many replies start from confirmation.",
                }
            ],
        }
    )

    assert normalized == {
        "priority_scene_numbers": [2, 4],
        "distribution_findings": [
            {
                "family": "repeated_confirmation_first_opening",
                "severity": "high",
                "scene_numbers": [2, 4],
                "description": "Too many replies start from confirmation.",
            }
        ],
    }

    merged = _merge_act_dialogue_report(
        base_report={"priority_scene_numbers": [1, 2]},
        llm_report=normalized,
    )
    assert merged["priority_scene_numbers"] == [1, 2, 4]
    assert merged["llm_priority_scene_numbers"] == [2, 4]


def test_build_and_normalize_episode_seam_audit_payload() -> None:
    segments = [
        SimpleNamespace(
            act_number=1,
            scene_number=6,
            dialogue_json=[
                {"speaker": "B", "text": "Who stops them now?"},
                {"speaker": "A", "text": "That is where we go next."},
            ],
        ),
        SimpleNamespace(
            act_number=2,
            scene_number=1,
            dialogue_json=[
                {"speaker": "B", "text": "Who stops them from printing as much as they want?"},
                {"speaker": "A", "text": "The answer is legal, not magical."},
            ],
        ),
    ]
    payload = _build_episode_seam_audit_prompt_payload(
        segments=segments,
        scene_job_lookup={(1, 6): "handoff", (2, 1): "mechanism"},
        language="en",
    )

    assert payload == {
        "language": "en",
        "seams": [
            {
                "from_act_number": 1,
                "from_scene_number": 6,
                "from_scene_job": "handoff",
                "to_act_number": 2,
                "target_scene_number": 1,
                "to_scene_job": "mechanism",
                "previous_scene_tail": ["Who stops them now?", "That is where we go next."],
                "next_scene_opening": [
                    "Who stops them from printing as much as they want?",
                    "The answer is legal, not magical.",
                ],
            }
        ],
    }

    normalized = _normalize_episode_seam_audit(
        {
            "seams": [
                {
                    "from_act_number": 1,
                    "to_act_number": 2,
                    "target_scene_number": 1,
                    "status": "replay_question",
                    "severity": "high",
                    "description": "The new act re-asks the previous act's question.",
                }
            ]
        }
    )
    assert normalized == [
        {
            "from_act_number": 1,
            "to_act_number": 2,
            "target_scene_number": 1,
            "status": "replay_question",
            "severity": "high",
            "description": "The new act re-asks the previous act's question.",
        }
    ]
    assert _seam_reports_by_target(normalized) == {
        (2, 1): {
            "from_act_number": 1,
            "to_act_number": 2,
            "target_scene_number": 1,
            "status": "replay_question",
            "severity": "high",
            "description": "The new act re-asks the previous act's question.",
        }
    }


def test_build_targeted_seam_audit_prompt_payload_uses_boundary_and_new_opening() -> None:
    segment = SimpleNamespace(act_number=5, scene_number=1)
    payload = _build_targeted_seam_audit_prompt_payload(
        segment=segment,
        language="ru",
        scene_job="payoff",
        boundary_context={
            "act_number": 4,
            "scene_number": 5,
            "last_lines": ["Last pressure.", "Why keep paying?"],
        },
        seam_finding={"from_scene_job": "synthesize"},
        polished_dialogue=[
            {"speaker": "B", "text": "Maybe that is the answer."},
            {"speaker": "A", "text": "Let us test that."},
        ],
    )

    assert payload == {
        "language": "ru",
        "seams": [
            {
                "from_act_number": 4,
                "from_scene_number": 5,
                "from_scene_job": "synthesize",
                "to_act_number": 5,
                "target_scene_number": 1,
                "to_scene_job": "payoff",
                "previous_scene_tail": ["Last pressure.", "Why keep paying?"],
                "next_scene_opening": ["Maybe that is the answer.", "Let us test that."],
            }
        ],
    }


def test_dialogue_act_monotony_signals_extract_repeated_distribution_findings() -> None:
    report = {
        "distribution_findings": [
            {
                "family": "repeated_explanatory_reset",
                "severity": "medium",
                "scene_numbers": [2, 3, 4],
                "description": "Same explanatory reset repeats.",
            },
            {
                "family": "other",
                "severity": "high",
                "scene_numbers": [1, 2],
                "description": "Too generic to route.",
            },
        ]
    }

    assert _dialogue_act_monotony_signals(report) == [
        {
            "family": "repeated_explanatory_reset",
            "severity": "medium",
            "scene_numbers": [2, 3, 4],
            "description": "Same explanatory reset repeats.",
        }
    ]


def test_build_multi_scene_polish_candidates_selects_contiguous_explanatory_reset_cluster() -> None:
    rows = [
        {
            "segment": SimpleNamespace(act_number=2, scene_number=2, dialogue_json=[{"speaker": "B", "text": "q?"}]),
            "can_intervene": True,
            "scene_job": "mechanism",
            "boundary_context": None,
            "issues": [],
            "intervention_targets": [],
        },
        {
            "segment": SimpleNamespace(act_number=2, scene_number=3, dialogue_json=[{"speaker": "B", "text": "q?"}]),
            "can_intervene": True,
            "scene_job": "mechanism",
            "boundary_context": None,
            "issues": [],
            "intervention_targets": [],
        },
        {
            "segment": SimpleNamespace(act_number=2, scene_number=4, dialogue_json=[{"speaker": "B", "text": "q?"}]),
            "can_intervene": True,
            "scene_job": "mechanism",
            "boundary_context": None,
            "issues": [],
            "intervention_targets": [],
        },
        {
            "segment": SimpleNamespace(act_number=2, scene_number=5, dialogue_json=[{"speaker": "B", "text": "q?"}]),
            "can_intervene": True,
            "scene_job": "mechanism",
            "boundary_context": None,
            "issues": [],
            "intervention_targets": [],
        },
    ]
    act_reports = {
        "2": {
            "act_number": 2,
            "distribution_findings": [
                {
                    "family": "repeated_explanatory_reset",
                    "severity": "medium",
                    "scene_numbers": [3, 4, 5],
                    "description": "Same reset repeats.",
                }
            ],
        }
    }

    candidates = _build_multi_scene_polish_candidates(audit_rows=rows, act_reports=act_reports)

    assert len(candidates) == 1
    assert candidates[0]["act_number"] == 2
    assert candidates[0]["scene_numbers"] == [3, 4, 5]
    assert candidates[0]["family"] == "repeated_explanatory_reset"
    assert rows[1]["multi_scene_candidate"]["scene_numbers"] == [3, 4, 5]


def test_build_dialogue_rhythm_metrics_uses_structural_signals_not_raw_word_counts() -> None:
    segments = [
        SimpleNamespace(
            act_number=2,
            scene_number=3,
            dialogue_json=[
                {"speaker": "B", "text": "Подожди, это вообще как работает?"},
                {"speaker": "A", "text": "Именно так правило и включается."},
            ],
        ),
        SimpleNamespace(
            act_number=2,
            scene_number=4,
            dialogue_json=[
                {"speaker": "B", "text": "Тогда что происходит в первый день?"},
                {"speaker": "A", "text": "То есть база сдвигается сразу."},
            ],
        ),
        SimpleNamespace(
            act_number=2,
            scene_number=5,
            dialogue_json=[
                {"speaker": "A", "text": "А теперь странность уже не в правиле, а в заводе."},
                {"speaker": "B", "text": "Вот это уже выглядит как институциональная oddity."},
            ],
        ),
    ]

    metrics = _build_dialogue_rhythm_metrics(segments)

    assert metrics["scene_count"] == 3
    assert metrics["speaker_question_share"] == {"A": 0.0, "B": 0.667}
    assert metrics["opening_shape_diversity"] == 2
    assert metrics["dominant_opening_speaker_share"] == 0.667
    assert metrics["b_question_a_opening_rate"] == 0.667
    assert metrics["explanatory_reset_opening_rate"] == 0.0


def test_build_multi_scene_polish_prompt_payload_includes_cluster_contract() -> None:
    candidate = {
        "act_number": 2,
        "family": "repeated_explanatory_reset",
        "severity": "medium",
        "scene_numbers": [3, 4, 5],
        "description": "Same mechanism reset repeats.",
        "rows": [
            {
                "segment": SimpleNamespace(
                    scene_number=3,
                    dialogue_json=[{"speaker": "B", "text": "How does it work?", "claim_ids": ["c1"]}],
                ),
                "scene_job": "mechanism",
                "boundary_context": {"last_line": "Previous line."},
                "issues": [{"type": "repetitive_agreement_shape"}],
                "intervention_targets": ["act:repeated_explanatory_reset"],
            }
        ],
    }

    payload = _build_multi_scene_polish_prompt_payload(
        candidate=candidate,
        language="ru",
        act_repetition_report={"dominant_opening_speaker": "B"},
    )

    assert payload["cluster_issue"]["family"] == "repeated_explanatory_reset"
    assert payload["scenes"][0]["scene_number"] == 3
    assert payload["success_criteria"][1] == "Do not let all scenes open with the same skeptical-question reset."


def test_finalize_dialogue_seam_reports_prefers_post_polish_recheck() -> None:
    segment = SimpleNamespace(
        act_number=5,
        scene_number=1,
        audit_findings={
            "dialogue": {
                "post_polish_seam_finding": {
                    "from_act_number": 4,
                    "to_act_number": 5,
                    "target_scene_number": 1,
                    "status": "acceptable_handoff",
                    "severity": "low",
                    "description": "Fixed.",
                }
            }
        },
    )
    final = _finalize_dialogue_seam_reports(
        seam_reports=[
            {
                "from_act_number": 4,
                "to_act_number": 5,
                "target_scene_number": 1,
                "status": "replay_question",
                "severity": "high",
                "description": "Old seam.",
            }
        ],
        audit_rows=[{"segment": segment}],
    )

    assert final == [
        {
            "from_act_number": 4,
            "to_act_number": 5,
            "target_scene_number": 1,
            "status": "acceptable_handoff",
            "severity": "low",
            "description": "Fixed.",
        }
    ]


def test_dialogue_polish_resolves_targets_requires_material_change_for_non_opener_repairs() -> None:
    original = [
        {"speaker": "B", "text": "So it is basically the same question?", "claim_ids": []},
        {"speaker": "A", "text": "Exactly. That is the structure.", "claim_ids": []},
    ]

    assert (
        _dialogue_polish_resolves_targets(
            original_dialogue=original,
            polished_dialogue=original,
            issues=[{"type": "repetitive_agreement_shape", "severity": "medium", "scope": "scene_polish"}],
            boundary_context=None,
            act_repetition_report=None,
            seam_finding=None,
            intervention_targets=["local:repetitive_agreement_shape"],
        )
        is False
    )

    assert (
        _dialogue_polish_resolves_targets(
            original_dialogue=original,
            polished_dialogue=[
                {"speaker": "B", "text": "Then the structure is doing the work, not trust.", "claim_ids": []},
                {"speaker": "A", "text": "Right, and that changes what the listener should notice.", "claim_ids": []},
            ],
            issues=[{"type": "repetitive_agreement_shape", "severity": "medium", "scope": "scene_polish"}],
            boundary_context=None,
            act_repetition_report=None,
            seam_finding=None,
            intervention_targets=["local:repetitive_agreement_shape"],
        )
        is True
    )


def test_dialogue_polish_resolves_targets_rejects_cosmetic_comprehension_repair() -> None:
    original = [
        {"speaker": "A", "text": "They changed it.", "claim_ids": []},
        {"speaker": "B", "text": "Why?", "claim_ids": []},
    ]

    assert (
        _dialogue_polish_resolves_targets(
            original_dialogue=original,
            polished_dialogue=[
                {"speaker": "A", "text": "They suddenly changed it.", "claim_ids": []},
                {"speaker": "B", "text": "Why exactly?", "claim_ids": []},
            ],
            issues=[{"type": "undefined_actor", "severity": "medium", "scope": "scene_polish"}],
            boundary_context=None,
            act_repetition_report=None,
            seam_finding=None,
            intervention_targets=["local:undefined_actor"],
        )
        is False
    )


def test_dialogue_polish_resolves_targets_accepts_substantive_comprehension_repair() -> None:
    original = [
        {"speaker": "A", "text": "They changed it.", "claim_ids": []},
        {"speaker": "B", "text": "Why?", "claim_ids": []},
    ]

    assert (
        _dialogue_polish_resolves_targets(
            original_dialogue=original,
            polished_dialogue=[
                {"speaker": "A", "text": "The Hong Kong government changed the peg rule.", "claim_ids": []},
                {"speaker": "B", "text": "Why would the government do that in the middle of panic?", "claim_ids": []},
            ],
            issues=[{"type": "undefined_actor", "severity": "medium", "scope": "scene_polish"}],
            boundary_context=None,
            act_repetition_report=None,
            seam_finding=None,
            intervention_targets=["local:undefined_actor"],
        )
        is True
    )


def test_dialogue_polish_resolves_targets_rejects_unexplained_term_relabel() -> None:
    original = [
        {"speaker": "A", "text": "Then the LERS tightened.", "claim_ids": []},
        {"speaker": "B", "text": "Why?", "claim_ids": []},
    ]

    assert (
        _dialogue_polish_resolves_targets(
            original_dialogue=original,
            polished_dialogue=[
                {"speaker": "A", "text": "Then the LERS corridor tightened sharply.", "claim_ids": []},
                {"speaker": "B", "text": "Why so quickly?", "claim_ids": []},
            ],
            issues=[{"type": "unexplained_term", "severity": "medium", "scope": "scene_polish"}],
            boundary_context=None,
            act_repetition_report=None,
            seam_finding=None,
            intervention_targets=["local:unexplained_term"],
            scene_metadata={"new_terms_to_introduce": ["LERS"]},
        )
        is False
    )


def test_dialogue_polish_resolves_targets_accepts_substantive_term_explanation() -> None:
    original = [
        {"speaker": "A", "text": "Then the LERS tightened.", "claim_ids": []},
        {"speaker": "B", "text": "Why?", "claim_ids": []},
    ]

    assert (
        _dialogue_polish_resolves_targets(
            original_dialogue=original,
            polished_dialogue=[
                {
                    "speaker": "A",
                    "text": "Then the LERS, Hong Kong's exchange-rate corridor, tightened sharply.",
                    "claim_ids": [],
                },
                {"speaker": "B", "text": "Why did that corridor tighten so quickly?", "claim_ids": []},
            ],
            issues=[{"type": "unexplained_term", "severity": "medium", "scope": "scene_polish"}],
            boundary_context=None,
            act_repetition_report=None,
            seam_finding=None,
            intervention_targets=["local:unexplained_term"],
            scene_metadata={"new_terms_to_introduce": ["LERS"]},
        )
        is True
    )


@pytest.mark.asyncio
async def test_attempt_dialogue_polish_propagates_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise_stop(**_: object) -> str:
        raise StepExecutionStopped("stop")

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _raise_stop)

    with pytest.raises(StepExecutionStopped):
        await _attempt_dialogue_polish(
            db=None,
            run_id="run-1",
            segment=SimpleNamespace(
                act_number=1,
                scene_number=2,
                dialogue_json=[{"speaker": "A", "text": "This scene has enough text to be audited.", "claim_ids": ["c1"]}],
                revision_count=0,
                audit_findings={},
            ),
            language="en",
            boundary_context={"last_line": "Previous scene."},
            scene_job="stress_test",
            issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
            act_repetition_report=None,
        )


@pytest.mark.asyncio
async def test_attempt_dialogue_polish_preserves_line_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_polished(**_: object) -> str:
        return """
        {
          "dialogue": [
            {"speaker": "A", "text": "A revised opener.", "claim_ids": ["c1"]},
            {"speaker": "B", "text": "The unchanged line.", "claim_ids": ["c2"]}
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_polished)

    segment = SimpleNamespace(
        act_number=1,
        scene_number=2,
        dialogue_json=[
            {
                "speaker": "A",
                "text": "The old opener.",
                "claim_ids": ["c1"],
                "emotion": "serious",
                "ssml_hints": {"pause_ms": 100},
            },
            {
                "speaker": "B",
                "text": "The unchanged line.",
                "claim_ids": ["c2"],
                "emotion": "skeptical",
                "ssml_hints": {"pause_ms": 220},
            },
        ],
        revision_count=0,
        audit_findings={"dialogue": {"issues": [{"type": "scene_opening_echo"}]}},
    )

    accepted = await _attempt_dialogue_polish(
        db=_DummyDb(),
        run_id="run-1",
        segment=segment,
        language="en",
        boundary_context={"last_line": "Previous scene."},
        scene_job="stress_test",
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        act_repetition_report=None,
    )

    assert accepted is True
    assert segment.dialogue_json == [
        {
            "speaker": "A",
            "text": "A revised opener.",
            "claim_ids": ["c1"],
            "emotion": "serious",
            "ssml_hints": {"pause_ms": 100},
        },
        {
            "speaker": "B",
            "text": "The unchanged line.",
            "claim_ids": ["c2"],
            "emotion": "skeptical",
            "ssml_hints": {"pause_ms": 220},
        },
    ]


@pytest.mark.asyncio
async def test_attempt_dialogue_polish_shortens_claim_ids_for_llm_and_restores_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    claim_id_1 = "11111111-1111-1111-1111-111111111111"
    claim_id_2 = "22222222-2222-2222-2222-222222222222"
    prompts: list[dict] = []

    async def _return_polished(**kwargs: object) -> str:
        payload = __import__("json").loads(kwargs["prompt"])
        prompts.append(payload)
        return """
        {
          "dialogue": [
            {"speaker": "A", "text": "A revised opener.", "claim_ids": ["c1"]},
            {"speaker": "B", "text": "The unchanged line.", "claim_ids": ["c2"]}
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_polished)

    segment = SimpleNamespace(
        act_number=1,
        scene_number=2,
        dialogue_json=[
            {
                "speaker": "A",
                "text": "The old opener.",
                "claim_ids": [claim_id_1],
                "emotion": "serious",
                "ssml_hints": {"pause_ms": 100},
            },
            {
                "speaker": "B",
                "text": "The unchanged line.",
                "claim_ids": [claim_id_2],
                "emotion": "skeptical",
                "ssml_hints": {"pause_ms": 220},
            },
        ],
        revision_count=0,
        audit_findings={"dialogue": {"issues": [{"type": "scene_opening_echo"}]}},
    )

    accepted = await _attempt_dialogue_polish(
        db=_DummyDb(),
        run_id="run-1",
        segment=segment,
        language="en",
        boundary_context={"last_line": "Previous scene."},
        scene_job="stress_test",
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        act_repetition_report=None,
    )

    assert accepted is True
    assert prompts[0]["dialogue"][0]["claim_ids"] == ["c1"]
    assert prompts[0]["dialogue"][1]["claim_ids"] == ["c2"]
    assert segment.dialogue_json[0]["claim_ids"] == [claim_id_1]
    assert segment.dialogue_json[1]["claim_ids"] == [claim_id_2]


@pytest.mark.asyncio
async def test_attempt_dialogue_polish_normalizes_named_host_speakers(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_polished(**_: object) -> str:
        return """
        {
          "dialogue": [
            {"speaker": "Марина", "text": "A revised opener.", "claim_ids": ["c1"]},
            {"speaker": "Дима", "text": "The unchanged line.", "claim_ids": ["c2"]}
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_polished)

    segment = SimpleNamespace(
        act_number=1,
        scene_number=2,
        dialogue_json=[
            {
                "speaker": "A",
                "text": "The old opener.",
                "claim_ids": ["c1"],
                "emotion": "serious",
                "ssml_hints": {"pause_ms": 100},
            },
            {
                "speaker": "B",
                "text": "The unchanged line.",
                "claim_ids": ["c2"],
                "emotion": "skeptical",
                "ssml_hints": {"pause_ms": 220},
            },
        ],
        revision_count=0,
        audit_findings={"dialogue": {"issues": [{"type": "scene_opening_echo"}]}},
    )

    accepted = await _attempt_dialogue_polish(
        db=_DummyDb(),
        run_id="run-1",
        segment=segment,
        language="ru",
        boundary_context={"last_line": "Earlier punchline."},
        scene_job="stress_test",
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        act_repetition_report=None,
        speaker_aliases={"марина": "A", "дима": "B"},
    )

    assert accepted is True
    assert [line["speaker"] for line in segment.dialogue_json] == ["A", "B"]


@pytest.mark.asyncio
async def test_attempt_dialogue_polish_accepts_changed_line_count_when_output_carries_delivery_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_polished(**_: object) -> str:
        return """
        {
          "dialogue": [
                {
                  "speaker": "B",
                  "text": "But the real constraint was legal.",
                  "claim_ids": [],
                  "emotion": "sharp",
                  "ssml_hints": {"pause_ms": 140}
                },
                {
                  "speaker": "A",
                  "text": "The law boxed them in before the press ever moved.",
                  "claim_ids": [],
                  "emotion": "measured",
                  "ssml_hints": {"pause_ms": 180}
                }
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_polished)
    async def _return_good_seam(**_: object) -> str:
        return """
        {
          "seams": [
            {
              "from_act_number": 1,
              "to_act_number": 2,
              "target_scene_number": 1,
              "status": "acceptable_handoff",
              "severity": "low",
              "description": "The seam now advances."
            }
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_haiku_structured", _return_good_seam)

    segment = SimpleNamespace(
        act_number=2,
        scene_number=1,
        dialogue_json=[
            {
                "speaker": "A",
                "text": "Who stops them from printing as much as they want?",
                "claim_ids": [],
                "emotion": "urgent",
                "ssml_hints": {"pause_ms": 110},
            }
        ],
        revision_count=0,
        audit_findings={"dialogue": {"issues": [{"type": "scene_opening_echo"}]}},
    )

    accepted = await _attempt_dialogue_polish(
        db=_DummyDb(),
        run_id="run-1",
        segment=segment,
        language="en",
        boundary_context={
            "act_number": 1,
            "scene_number": 6,
            "last_lines": ["Who stops them now?"],
            "last_line": "Who stops them now?",
        },
        scene_job="mechanism",
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        act_repetition_report=None,
        seam_finding={"status": "replay_question", "severity": "high"},
        intervention_targets=["seam:replay_question"],
    )

    assert accepted is True
    assert segment.dialogue_json == [
            {
                "speaker": "B",
                "text": "But the real constraint was legal.",
                "claim_ids": [],
                "emotion": "sharp",
                "ssml_hints": {"pause_ms": 140},
            },
            {
                "speaker": "A",
                "text": "The law boxed them in before the press ever moved.",
                "claim_ids": [],
                "emotion": "measured",
                "ssml_hints": {"pause_ms": 180},
            },
    ]
    assert segment.audit_findings["dialogue"]["post_polish_seam_finding"]["status"] == "acceptable_handoff"


@pytest.mark.asyncio
async def test_attempt_multi_scene_polish_accepts_cluster_with_more_varied_openers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_cluster_polish(**_: object) -> str:
        return """
        {
          "scenes": [
            {
              "scene_number": 3,
              "dialogue": [
                {"speaker": "B", "text": "Не в печати фокус, а в том, что банк сначала приносит доллары США.", "claim_ids": ["c1"]},
                {"speaker": "A", "text": "И только после этого получает сертификаты задолженности.", "claim_ids": ["c2"]}
              ]
            },
            {
              "scene_number": 4,
              "dialogue": [
                {"speaker": "A", "text": "А теперь представь первый же день оттока капитала: база сжимается автоматически.", "claim_ids": ["c3"]},
                {"speaker": "B", "text": "То есть правило реагирует раньше, чем любой чиновник успеет придумать манёвр.", "claim_ids": ["c4"]}
              ]
            },
            {
              "scene_number": 5,
              "dialogue": [
                {"speaker": "A", "text": "И последняя странность: печатают разные банки, а физически всё идёт через одну фабрику.", "claim_ids": ["c5"]},
                {"speaker": "B", "text": "Вот это уже делает систему не просто жёсткой, а ещё и очень сконструированной.", "claim_ids": ["c6"]}
              ]
            }
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_cluster_polish)

    rows = [
        {
            "segment": SimpleNamespace(
                act_number=2,
                scene_number=3,
                dialogue_json=[
                    {"speaker": "B", "text": "Просто печатает — и всё?", "claim_ids": ["c1"], "emotion": "skeptical"},
                    {"speaker": "A", "text": "Нет. Сначала банк приносит доллары США.", "claim_ids": ["c2"], "emotion": "measured"},
                ],
                revision_count=0,
                audit_findings={"dialogue": {}},
            ),
            "scene_job": "mechanism",
            "boundary_context": None,
            "issues": [{"type": "repetitive_agreement_shape", "severity": "medium", "scope": "scene_polish"}],
            "intervention_targets": ["act:repeated_explanatory_reset"],
        },
        {
            "segment": SimpleNamespace(
                act_number=2,
                scene_number=4,
                dialogue_json=[
                    {"speaker": "B", "text": "Что происходит в первый же день?", "claim_ids": ["c3"], "emotion": "skeptical"},
                    {"speaker": "A", "text": "Именно в первый день база сдвигается.", "claim_ids": ["c4"], "emotion": "measured"},
                ],
                revision_count=0,
                audit_findings={"dialogue": {}},
            ),
            "scene_job": "cost",
            "boundary_context": None,
            "issues": [{"type": "repetitive_agreement_shape", "severity": "medium", "scope": "scene_polish"}],
            "intervention_targets": ["act:repeated_explanatory_reset"],
        },
        {
            "segment": SimpleNamespace(
                act_number=2,
                scene_number=5,
                dialogue_json=[
                    {"speaker": "B", "text": "И все несут заказ в одно место?", "claim_ids": ["c5"], "emotion": "skeptical"},
                    {"speaker": "A", "text": "Логично, если знаешь, кому принадлежит завод.", "claim_ids": ["c6"], "emotion": "measured"},
                ],
                revision_count=0,
                audit_findings={"dialogue": {}},
            ),
            "scene_job": "institutional_oddity",
            "boundary_context": None,
            "issues": [{"type": "repetitive_agreement_shape", "severity": "medium", "scope": "scene_polish"}],
            "intervention_targets": ["act:repeated_explanatory_reset"],
        },
    ]
    candidate = {
        "act_number": 2,
        "family": "repeated_explanatory_reset",
        "severity": "medium",
        "scene_numbers": [3, 4, 5],
        "description": "Same explanatory reset repeats.",
        "rows": rows,
    }

    accepted = await _attempt_multi_scene_polish(
        db=_DummyDb(),
        run_id="run-1",
        candidate=candidate,
        language="ru",
        speaker_aliases=None,
        act_repetition_report={"dominant_opening_speaker": "B"},
    )

    assert accepted is True
    assert rows[1]["segment"].dialogue_json[0]["speaker"] == "A"
    assert rows[1]["segment"].revision_count == 1
    assert rows[2]["segment"].audit_findings["dialogue"]["multi_scene_polish_applied"] is True


@pytest.mark.asyncio
async def test_attempt_multi_scene_polish_rejects_cross_scene_claim_borrowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_borrowed_claims(**_: object) -> str:
        return """
        {
          "scenes": [
            {
              "scene_number": 3,
              "dialogue": [
                {"speaker": "B", "text": "Scene three now carries both claims.", "claim_ids": ["c1", "c2"]}
              ]
            },
            {
              "scene_number": 4,
              "dialogue": [
                {"speaker": "A", "text": "Scene four keeps its own claim.", "claim_ids": ["c1"]}
              ]
            },
            {
              "scene_number": 5,
              "dialogue": [
                {"speaker": "A", "text": "Scene five keeps its own claim too.", "claim_ids": ["c1"]}
              ]
            }
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_borrowed_claims)

    rows = [
        {
            "segment": SimpleNamespace(
                act_number=2,
                scene_number=3,
                dialogue_json=[{"speaker": "B", "text": "Scene three.", "claim_ids": ["claim-a"]}],
                revision_count=0,
                audit_findings={"dialogue": {}},
            ),
            "scene_job": "mechanism",
            "boundary_context": None,
            "issues": [],
            "intervention_targets": ["act:repeated_explanatory_reset"],
        },
        {
            "segment": SimpleNamespace(
                act_number=2,
                scene_number=4,
                dialogue_json=[{"speaker": "A", "text": "Scene four.", "claim_ids": ["claim-b"]}],
                revision_count=0,
                audit_findings={"dialogue": {}},
            ),
            "scene_job": "cost",
            "boundary_context": None,
            "issues": [],
            "intervention_targets": ["act:repeated_explanatory_reset"],
        },
        {
            "segment": SimpleNamespace(
                act_number=2,
                scene_number=5,
                dialogue_json=[{"speaker": "A", "text": "Scene five.", "claim_ids": ["claim-c"]}],
                revision_count=0,
                audit_findings={"dialogue": {}},
            ),
            "scene_job": "oddity",
            "boundary_context": None,
            "issues": [],
            "intervention_targets": ["act:repeated_explanatory_reset"],
        },
    ]

    accepted = await _attempt_multi_scene_polish(
        db=_DummyDb(),
        run_id="run-1",
        candidate={
            "act_number": 2,
            "family": "repeated_explanatory_reset",
            "severity": "medium",
            "scene_numbers": [3, 4, 5],
            "description": "Repeated reset.",
            "rows": rows,
        },
        language="en",
        speaker_aliases=None,
        act_repetition_report=None,
    )

    assert accepted is False
    assert rows[0]["segment"].revision_count == 0


@pytest.mark.asyncio
async def test_attempt_multi_scene_polish_rejects_opener_variation_when_explanatory_reset_pattern_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_same_reset_pattern(**_: object) -> str:
        return """
        {
          "scenes": [
            {
              "scene_number": 3,
              "dialogue": [
                {"speaker": "A", "text": "A different opener wording.", "claim_ids": []},
                {"speaker": "A", "text": "The explanation still starts immediately.", "claim_ids": []},
                {"speaker": "B", "text": "So the same practical question follows?", "claim_ids": []},
                {"speaker": "A", "text": "And the same explanation lands again.", "claim_ids": []}
              ]
            },
            {
              "scene_number": 4,
              "dialogue": [
                {"speaker": "B", "text": "What happens next?", "claim_ids": []},
                {"speaker": "A", "text": "The mechanism answer comes right away.", "claim_ids": []},
                {"speaker": "B", "text": "So the listener is still being reset?", "claim_ids": []},
                {"speaker": "A", "text": "Yes, the explanatory chain still dominates.", "claim_ids": []}
              ]
            },
            {
              "scene_number": 5,
              "dialogue": [
                {"speaker": "B", "text": "And what about the factory?", "claim_ids": []},
                {"speaker": "A", "text": "Again the explanation arrives immediately.", "claim_ids": []},
                {"speaker": "B", "text": "So it is still the same cadence?", "claim_ids": []},
                {"speaker": "A", "text": "It still resolves the same way.", "claim_ids": []}
              ]
            }
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_same_reset_pattern)

    rows = []
    for scene_number in (3, 4, 5):
        rows.append(
            {
                "segment": SimpleNamespace(
                    act_number=2,
                    scene_number=scene_number,
                    dialogue_json=[
                        {"speaker": "B", "text": "How does this work?", "claim_ids": []},
                        {"speaker": "A", "text": "Here is the first explanation.", "claim_ids": []},
                        {"speaker": "B", "text": "So what happens in practice?", "claim_ids": []},
                        {"speaker": "A", "text": "Here is the second explanation.", "claim_ids": []},
                    ],
                    revision_count=0,
                    audit_findings={"dialogue": {}},
                ),
                "scene_job": "mechanism",
                "boundary_context": None,
                "issues": [],
                "intervention_targets": ["act:repeated_explanatory_reset"],
            }
        )

    accepted = await _attempt_multi_scene_polish(
        db=_DummyDb(),
        run_id="run-1",
        candidate={
            "act_number": 2,
            "family": "repeated_explanatory_reset",
            "severity": "medium",
            "scene_numbers": [3, 4, 5],
            "description": "Repeated reset.",
            "rows": rows,
        },
        language="en",
        speaker_aliases=None,
        act_repetition_report=None,
    )

    assert accepted is False
    assert all(row["segment"].revision_count == 0 for row in rows)


@pytest.mark.asyncio
async def test_attempt_dialogue_polish_rejects_when_post_polish_seam_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_polished(**_: object) -> str:
        return """
        {
          "dialogue": [
            {"speaker": "B", "text": "But that old question is still hanging.", "claim_ids": []},
            {"speaker": "A", "text": "So let us restate it safely.", "claim_ids": []}
          ]
        }
        """

    async def _return_bad_seam(**_: object) -> str:
        return """
        {
          "seams": [
            {
              "from_act_number": 4,
              "to_act_number": 5,
              "target_scene_number": 1,
              "status": "replay_question",
              "severity": "high",
              "description": "Still replaying the old question."
            }
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_polished)
    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_haiku_structured", _return_bad_seam)

    segment = SimpleNamespace(
        act_number=5,
        scene_number=1,
        dialogue_json=[
            {"speaker": "B", "text": "Why keep paying for this system?", "claim_ids": []},
            {"speaker": "A", "text": "That is the question.", "claim_ids": []},
        ],
        revision_count=0,
        audit_findings={"dialogue": {}},
    )

    accepted = await _attempt_dialogue_polish(
        db=_DummyDb(),
        run_id="run-1",
        segment=segment,
        language="en",
        boundary_context={
            "act_number": 4,
            "scene_number": 5,
            "last_lines": ["Why keep paying for this system?"],
            "last_line": "Why keep paying for this system?",
        },
        scene_job="payoff",
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        act_repetition_report=None,
        seam_finding={
            "from_act_number": 4,
            "to_act_number": 5,
            "target_scene_number": 1,
            "status": "replay_question",
            "severity": "high",
        },
        intervention_targets=["seam:replay_question"],
    )

    assert accepted is False
    assert segment.revision_count == 0


@pytest.mark.asyncio
async def test_attempt_dialogue_polish_rejects_priority_only_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyDb:
        async def flush(self) -> None:
            return None

    async def _return_same_dialogue(**_: object) -> str:
        return """
        {
          "dialogue": [
            {"speaker": "A", "text": "The opener stays exactly the same.", "claim_ids": ["c1"]},
            {"speaker": "B", "text": "And the follow-up line also stays the same.", "claim_ids": []}
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_same_dialogue)

    segment = SimpleNamespace(
        act_number=2,
        scene_number=3,
        dialogue_json=[
            {"speaker": "A", "text": "The opener stays exactly the same.", "claim_ids": ["c1"]},
            {"speaker": "B", "text": "And the follow-up line also stays the same.", "claim_ids": []},
        ],
        revision_count=0,
        audit_findings={},
    )

    accepted = await _attempt_dialogue_polish(
        db=_DummyDb(),
        run_id="run-1",
        segment=segment,
        language="en",
        boundary_context=None,
        scene_job="mechanism",
        issues=[],
        act_repetition_report={"priority_scene_numbers": [3]},
        seam_finding=None,
        intervention_targets=[],
    )

    assert accepted is False
    assert segment.revision_count == 0
    assert segment.dialogue_json == [
        {"speaker": "A", "text": "The opener stays exactly the same.", "claim_ids": ["c1"]},
        {"speaker": "B", "text": "And the follow-up line also stays the same.", "claim_ids": []},
    ]


@pytest.mark.asyncio
async def test_augment_act_dialogue_reports_with_llm_propagates_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise_stop(_: dict[str, object]) -> dict[str, object]:
        raise StepExecutionStopped("stop")

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt._run_act_dialogue_audit", _raise_stop)

    with pytest.raises(StepExecutionStopped):
        await _augment_act_dialogue_reports_with_llm(
            audit_rows=[
                {
                    "segment": SimpleNamespace(
                        act_number=1,
                        scene_number=1,
                        dialogue_json=[{"speaker": "A", "text": "Open."}, {"speaker": "B", "text": "Close."}],
                    ),
                    "issues": [],
                    "priority_score": 0,
                }
            ],
            act_reports={"1": {"act_number": 1, "scene_count": 1}},
            language="en",
        )


@pytest.mark.asyncio
async def test_evaluate_and_revolt_propagates_stop_from_dialogue_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummySession:
        async def __aenter__(self) -> "_DummySession":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def _load_segments_for_audit(*_: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                act_number=1,
                scene_number=1,
                dialogue_json=[
                    {"speaker": "A", "text": "This scene has enough text to trigger the audit path.", "claim_ids": ["c1"]},
                    {"speaker": "B", "text": "And it keeps going so the payload is not skipped.", "claim_ids": []},
                ],
                engagement_score=None,
                revision_count=0,
                audit_findings={},
            )
        ]

    async def _load_scene_job_lookup(*_: object) -> dict[tuple[int, int], str]:
        return {}

    async def _raise_stop(_: dict[str, object]) -> dict[str, object]:
        raise StepExecutionStopped("stop")

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.async_session_factory", lambda: _DummySession())
    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt._load_segments_for_audit", _load_segments_for_audit)
    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt._load_scene_job_lookup", _load_scene_job_lookup)
    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt._run_dialogue_audit", _raise_stop)
    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.publish_event", lambda *args, **kwargs: None)

    with pytest.raises(StepExecutionStopped):
        await evaluate_and_revolt(
            {
                "generation_run_id": "run-1",
                "script_segment_ids": ["seg-1"],
                "iteration_counts": {},
                "quality_signals": {},
                "language": "en",
                "manifest_id": None,
            }
        )
