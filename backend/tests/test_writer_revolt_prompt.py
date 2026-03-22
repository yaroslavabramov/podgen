from types import SimpleNamespace

import pytest

from app.pipeline.phase6_writing.writer_revolt import (
    ACT_AUDIT_SYSTEM,
    EVAL_SYSTEM,
    POLISH_SYSTEM,
    _attempt_dialogue_polish,
    _augment_act_dialogue_reports_with_llm,
    _build_act_dialogue_reports,
    _build_act_dialogue_audit_prompt_payload,
    _build_boundary_context,
    _build_dialogue_audit_prompt_payload,
    _build_dialogue_polish_prompt_payload,
    _build_episode_seam_audit_prompt_payload,
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
    assert '"severity": "low|medium|high"' in EVAL_SYSTEM
    assert '"scope": "scene_polish|scene_rewrite|act_warning"' in EVAL_SYSTEM
    assert "closely repeating or paraphrasing the previous scene's punchline" in EVAL_SYSTEM
    assert "topic bridge or thesis-to-example progression is not echo by itself" in EVAL_SYSTEM
    assert "The payload includes `language`." in EVAL_SYSTEM
    assert "Judge the scene as spoken dialogue in that language" in EVAL_SYSTEM
    assert "Replace the current single-scene score gate" not in EVAL_SYSTEM
    assert "The payload includes `language`; keep the scene natural for that language and spoken register." in POLISH_SYSTEM
    assert "Avoid translated-from-English phrasing or register drift." in POLISH_SYSTEM
    assert "Use the audit issues to improve scene flow, boundary handling, and conversational variety." in POLISH_SYSTEM
    assert "The payload may include an `act_repetition_report`" in POLISH_SYSTEM
    assert "The payload may include a `seam_finding`" in POLISH_SYSTEM
    assert "If you split, merge, or otherwise change line alignment" in POLISH_SYSTEM
    assert "If the boundary context already landed a punchline, do not reopen by paraphrasing it." in POLISH_SYSTEM
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
    ) == {
        "act_number": 3,
        "scene_number": 2,
        "language": "ru",
        "scene_job": "stress_test",
        "boundary_context": {"last_line": "Previous punchline."},
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
        boundary_context={"last_line": "Previous line."},
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
        act_repetition_report={"dominant_opening_speaker": "A"},
    ) == {
        "act_number": 4,
        "scene_number": 6,
        "language": "ru",
        "scene_job": "cost",
        "boundary_context": {"last_line": "Previous line."},
        "act_repetition_report": {"dominant_opening_speaker": "A"},
        "seam_finding": None,
        "intervention_targets": [],
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


def test_row_with_act_level_target_can_still_be_polished_without_local_medium_issue() -> None:
    row = {
        "issues": [{"type": "scene_opening_echo", "severity": "low", "scope": "act_warning"}],
        "intervention_targets": ["act:repeated_confirmation_first_opening"],
    }

    assert not _should_polish_dialogue(row["issues"])
    assert _row_should_polish_dialogue(row)


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
              "claim_ids": ["c1"],
              "emotion": "sharp",
              "ssml_hints": {"pause_ms": 140}
            },
            {
              "speaker": "A",
              "text": "The law boxed them in before the press ever moved.",
              "claim_ids": ["c1"],
              "emotion": "measured",
              "ssml_hints": {"pause_ms": 180}
            }
          ]
        }
        """

    monkeypatch.setattr("app.pipeline.phase6_writing.writer_revolt.call_sonnet_structured", _return_polished)

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
        boundary_context={"last_lines": ["Who stops them now?"], "last_line": "Who stops them now?"},
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
            "claim_ids": ["c1"],
            "emotion": "sharp",
            "ssml_hints": {"pause_ms": 140},
        },
        {
            "speaker": "A",
            "text": "The law boxed them in before the press ever moved.",
            "claim_ids": ["c1"],
            "emotion": "measured",
            "ssml_hints": {"pause_ms": 180},
        },
    ]


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
