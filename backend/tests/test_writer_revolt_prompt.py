from types import SimpleNamespace

from app.pipeline.phase6_writing.writer_revolt import (
    EVAL_SYSTEM,
    POLISH_SYSTEM,
    _build_boundary_context,
    _build_dialogue_audit_prompt_payload,
    _build_dialogue_polish_prompt_payload,
    _normalize_dialogue_issues,
    _should_polish_dialogue,
    _should_request_scene_rewrite,
)


def test_writer_revolt_prompt_tracks_boundary_context_and_structured_repair_scopes() -> None:
    assert "boundary_restatement" in EVAL_SYSTEM
    assert '"severity": "low|medium|high"' in EVAL_SYSTEM
    assert '"scope": "scene_polish|scene_rewrite|act_warning"' in EVAL_SYSTEM
    assert "scene-to-scene echo where the new scene opens by repeating the previous scene's punchline" in EVAL_SYSTEM
    assert "Replace the current single-scene score gate" not in EVAL_SYSTEM
    assert "Use the audit issues to improve scene flow, boundary handling, and conversational variety." in POLISH_SYSTEM
    assert "If the boundary context already landed a punchline, do not reopen by paraphrasing it." in POLISH_SYSTEM


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
        boundary_context=boundary_context,
        scene_job="stress_test",
    ) == {
        "act_number": 3,
        "scene_number": 2,
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
        scene_job="cost",
        boundary_context={"last_line": "Previous line."},
        issues=[{"type": "scene_opening_echo", "severity": "medium", "scope": "scene_polish"}],
    ) == {
        "act_number": 4,
        "scene_number": 6,
        "scene_job": "cost",
        "boundary_context": {"last_line": "Previous line."},
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
