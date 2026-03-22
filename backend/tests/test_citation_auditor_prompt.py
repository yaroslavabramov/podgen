import uuid
from types import SimpleNamespace

import pytest

from app.models.script_segment import AuditStatus
from app.pipeline.phase6_writing.citation_auditor import (
    AUDITOR_SYSTEM,
    LINE_REPAIR_SYSTEM,
    REPAIR_SYSTEM,
    _attempt_citation_repair,
    _build_citation_audit_prompt_payload,
    _build_citation_repair_attempt_plan,
    _build_citation_repair_prompt_payload,
    _build_scene_context,
    _build_failed_citation_audit,
    _citation_repair_improved,
    _changed_line_indexes,
    _has_blocking_citation_issues,
    _load_claims_map,
    _merge_scene_repair_metadata,
    _normalize_citation_issues,
    _relevant_reaudit_line_indexes,
    _scene_repair_preserves_unflagged_sensitive_lines,
    _select_citation_repair_mode,
    _sensitive_line_indexes,
    _post_filter_citation_issues,
    _run_citation_audit,
    _run_citation_audit_with_retry,
    _select_scene_claims_map,
    _should_attempt_citation_repair,
    _splice_repaired_lines,
    audit_citations,
)
from app.pipeline.step_control import StepExecutionStopped


def _line(text: str, claim_ids: list[str] | None = None, speaker: str = "A") -> dict:
    return {"speaker": speaker, "text": text, "claim_ids": claim_ids or []}


def test_citation_auditor_prompt_demands_severity_scope_and_repair() -> None:
    assert '"severity": "low|medium|high"' in AUDITOR_SYSTEM
    assert '"scope": "record|repair|block"' in AUDITOR_SYSTEM
    assert '"line_function": "assertive|question_open|question_with_premise|recap|transition|synthesis|other"' in AUDITOR_SYSTEM
    assert '"action_required": true' in AUDITOR_SYSTEM
    assert "unsupported_source_framing" in AUDITOR_SYSTEM
    assert "must be repaired or block audio" in AUDITOR_SYSTEM
    assert "quoted, sourced, or over-specific fragment" in AUDITOR_SYSTEM
    assert "The payload includes `language`." in AUDITOR_SYSTEM
    assert "Audit the dialogue in that language" in AUDITOR_SYSTEM
    assert "Questions, callbacks, recaps, scene transitions, and synthesis lines" in AUDITOR_SYSTEM
    assert "rewrite only the targeted lines" in LINE_REPAIR_SYSTEM.lower()
    assert "spoken podcast dialogue" in LINE_REPAIR_SYSTEM
    assert "The payload includes `language`" in LINE_REPAIR_SYSTEM
    assert "Do not default to bureaucratic, legalistic, or academic hedging" in LINE_REPAIR_SYSTEM
    assert "Do not turn an internal bridge into sign-off or next-episode teaser language" in LINE_REPAIR_SYSTEM
    assert "If the line already carries uncertainty, do not stack multiple hedges" in LINE_REPAIR_SYSTEM
    assert "If exact timing is unsupported, keep the event but lower the precision." in LINE_REPAIR_SYSTEM
    assert "If a mechanism explanation is mostly supported, keep the mechanism and narrow only the unsupported step." in LINE_REPAIR_SYSTEM
    assert "drop the unsupported framing and keep the supported substance" in LINE_REPAIR_SYSTEM
    assert "Fix only the citation and rigor issues that were flagged." in REPAIR_SYSTEM
    assert "Focus edits on lines flagged as `block` or `repair`" in REPAIR_SYSTEM
    assert "Preserve the scene's dramatic structure, pacing, and agency" in REPAIR_SYSTEM
    assert "If `available_claims` mark a claim as `verified`" in REPAIR_SYSTEM
    assert "Do not add new facts, institutions, dates, figures, or claims." in REPAIR_SYSTEM
    assert "Preserve the scene's `scene_job`, `scene_type`, and content function." in REPAIR_SYSTEM
    assert "Preserve natural spoken register in the target language" in REPAIR_SYSTEM
    assert "If a line already contains uncertainty, do not stack multiple hedges" in REPAIR_SYSTEM
    assert "keep the mechanism and narrow only the unsupported step" in REPAIR_SYSTEM
    assert "remove the unsupported framing and keep the supported substance" in REPAIR_SYSTEM
    assert "по имеющимся данным" not in LINE_REPAIR_SYSTEM
    assert "в соответствии с" not in LINE_REPAIR_SYSTEM
    assert "я должен оговориться" not in LINE_REPAIR_SYSTEM
    assert "по имеющимся данным" not in REPAIR_SYSTEM


def test_build_citation_audit_prompt_payload_keeps_policy_and_claim_context() -> None:
    payload = _build_citation_audit_prompt_payload(
        dialogue=[_line("Line", ["c1"])],
        claims_map={"c1": {"type": "fact", "verification": "verified", "statement": "Claim"}},
        forbidden_phrases=["game changer"],
        rigor_policy="No unsupported certainty.",
        language="ru",
        scene_context={
            "act_number": 2,
            "scene_number": 4,
            "scene_job": "contrast",
            "scene_type": "debate",
            "description": "Compare systems.",
        },
    )

    assert payload == {
        "dialogue": [_line("Line", ["c1"])],
        "scene_context": {
            "act_number": 2,
            "scene_number": 4,
            "scene_job": "contrast",
            "scene_type": "debate",
            "description": "Compare systems.",
        },
        "available_claims": {"c1": {"type": "fact", "verification": "verified", "statement": "Claim"}},
        "forbidden_phrases": ["game changer"],
        "rigor_policy": "No unsupported certainty.",
        "language": "ru",
    }


def test_build_scene_context_keeps_final_scene_flags() -> None:
    context = _build_scene_context(
        act_number=4,
        scene_number=6,
        scene_metadata={
            "scene_job": "cost",
            "scene_type": "takeaway",
            "description": "Final risk scene.",
            "is_final_scene_in_act": True,
            "is_final_scene_in_episode": True,
        },
    )

    assert context == {
        "act_number": 4,
        "scene_number": 6,
        "scene_job": "cost",
        "scene_type": "takeaway",
        "description": "Final risk scene.",
        "is_final_scene_in_act": True,
        "is_final_scene_in_episode": True,
    }


def test_build_citation_repair_prompt_payload_includes_flagged_lines_for_scene_repair() -> None:
    payload = _build_citation_repair_prompt_payload(
        repair_mode="scene_repair",
        scene_context={
            "act_number": 3,
            "scene_number": 4,
            "scene_job": "cost",
            "scene_type": "debate",
            "description": "Stress scene.",
            "is_final_scene_in_act": False,
            "is_final_scene_in_episode": False,
        },
        original_line_count=3,
        dialogue=[
            _line("Line one", ["c1"]),
            _line("Line two", ["c2"], speaker="B"),
            _line("Line three", []),
        ],
        claims_map={"c1": {"statement": "Claim one"}, "c2": {"statement": "Claim two"}},
        forbidden_phrases=[],
        rigor_policy="",
        language="ru",
        issues=[
            {"line_index": 1, "type": "missing_claim_link", "severity": "medium", "scope": "repair"},
            {"line_index": 2, "type": "missing_attribution", "severity": "low", "scope": "record"},
        ],
        target_line_indexes=[],
    )

    assert payload["flagged_line_indexes"] == [1]
    assert payload["line_roles"] == {0: "scene_opener", 1: "punchline", 2: "landing"}
    assert payload["flagged_lines"] == [
        {
            "line_index": 1,
            "dialogue": _line("Line two", ["c2"], speaker="B"),
            "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        }
    ]


def test_select_scene_claims_map_uses_dialogue_and_manifest_claim_ids_only() -> None:
    subset = _select_scene_claims_map(
        claims_map={
            "c1": {"statement": "Line claim"},
            "c2": {"statement": "Scene claim"},
            "c3": {"statement": "Episode-only noise"},
        },
        dialogue=[_line("Line", ["c1"])],
        scene_claim_ids=["c2"],
    )

    assert subset == {
        "c1": {"statement": "Line claim"},
        "c2": {"statement": "Scene claim"},
    }


class _FakeClaimsResult:
    def __init__(self, claims) -> None:
        self._claims = list(claims)

    def scalars(self):
        return self._claims


class _FakeClaimsDB:
    def __init__(self, claims) -> None:
        self._claims = list(claims)

    async def execute(self, _statement):
        return _FakeClaimsResult(self._claims)


@pytest.mark.asyncio
async def test_load_claims_map_keeps_full_claim_statement() -> None:
    claim = SimpleNamespace(
        id=uuid.uuid4(),
        claim_type="fact",
        verification_level="verified",
        statement="К весне 1842 года британская администрация признала хождение испанских и мексиканских серебряных долларов, а к 1860-м годам окончательно отказалась от попыток вытеснить уже сложившуюся денежную систему.",
        attribution=None,
    )

    claims_map = await _load_claims_map(_FakeClaimsDB([claim]), [str(claim.id)])

    assert claims_map == {
        str(claim.id): {
            "type": "fact",
            "verification": "verified",
            "statement": claim.statement,
            "attribution": None,
            "has_attribution": False,
            "supports_source_framing": False,
        }
    }


def test_splice_repaired_lines_preserves_metadata_on_unflagged_and_target_lines() -> None:
    original_dialogue = [
        {
            "speaker": "A",
            "text": "Линия один.",
            "claim_ids": ["c1"],
            "emotion": "curious",
            "ssml_hints": {"pause_ms": 120},
        },
        {
            "speaker": "B",
            "text": "Но кто тогда мешал печатать сколько угодно?",
            "claim_ids": ["c2"],
            "emotion": "skeptical",
            "ssml_hints": {"pause_ms": 240},
        },
        {
            "speaker": "A",
            "text": "Линия три.",
            "claim_ids": ["c3"],
            "emotion": "calm",
            "ssml_hints": {"pause_ms": 80},
        },
    ]

    spliced = _splice_repaired_lines(
        original_dialogue=original_dialogue,
        repaired_lines={
            1: {
                "speaker": "B",
                "text": "Тогда другой вопрос: что их ограничивало?",
                "claim_ids": [],
            }
        },
        target_line_indexes=[1],
    )

    assert spliced == [
        {
            "speaker": "A",
            "text": "Линия один.",
            "claim_ids": ["c1"],
            "emotion": "curious",
            "ssml_hints": {"pause_ms": 120},
        },
        {
            "speaker": "B",
            "text": "Тогда другой вопрос: что их ограничивало?",
            "claim_ids": [],
            "emotion": "skeptical",
            "ssml_hints": {"pause_ms": 240},
        },
        {
            "speaker": "A",
            "text": "Линия три.",
            "claim_ids": ["c3"],
            "emotion": "calm",
            "ssml_hints": {"pause_ms": 80},
        },
    ]
    assert spliced is not original_dialogue
    assert spliced[0] is not original_dialogue[0]
    assert spliced[1]["ssml_hints"] is not original_dialogue[1]["ssml_hints"]


def test_merge_scene_repair_metadata_preserves_matching_line_fields() -> None:
    original_dialogue = [
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
    ]

    merged = _merge_scene_repair_metadata(
        original_dialogue=original_dialogue,
        repaired_dialogue=[
            {"speaker": "A", "text": "A revised opener.", "claim_ids": ["c1"]},
            {"speaker": "B", "text": "The unchanged line.", "claim_ids": ["c2", "c3"]},
        ],
    )

    assert merged == [
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
            "claim_ids": ["c2", "c3"],
            "emotion": "skeptical",
            "ssml_hints": {"pause_ms": 220},
        },
    ]


def test_select_citation_repair_mode_prefers_line_repair_for_sensitive_lines() -> None:
    dialogue = [
        _line("So what changed first?", []),
        _line("The market broke the old assumption.", ["c1"], speaker="B"),
        _line("And that pressure reached the peg immediately.", ["c2"]),
    ]

    assert _select_citation_repair_mode(
        dialogue=dialogue,
        issues=[
            {
                "line_index": 2,
                "type": "missing_claim_link",
                "severity": "medium",
                "scope": "repair",
                "action_required": True,
            }
        ],
    ) == ("line_repair", [2])


def test_select_citation_repair_mode_escalates_when_sensitive_targets_exceed_line_limit() -> None:
    dialogue = [
        _line("Opening premise.", ["c1"]),
        _line("Middle detail.", ["c2"], speaker="B"),
        _line("Landing implication.", ["c3"]),
    ]

    issues = [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "medium",
            "scope": "repair",
            "action_required": True,
        },
        {
            "line_index": 1,
            "type": "missing_claim_link",
            "severity": "medium",
            "scope": "repair",
            "action_required": True,
        },
        {
            "line_index": 2,
            "type": "missing_claim_link",
            "severity": "medium",
            "scope": "repair",
            "action_required": True,
        },
    ]

    assert _select_citation_repair_mode(dialogue=dialogue, issues=issues) == ("scene_repair", [])
    assert _build_citation_repair_attempt_plan(
        dialogue=dialogue,
        scene_context={"act_number": 1, "scene_number": 1},
        issues=issues,
        max_attempts=1,
    ) == [{"mode": "scene_repair", "target_line_indexes": []}]


def test_changed_line_indexes_and_relevant_reaudit_indexes_focus_on_changed_lines() -> None:
    original_dialogue = [
        _line("Old opener.", ["c1"]),
        _line("Middle line.", ["c2"], speaker="B"),
        _line("Old ending.", ["c3"]),
    ]
    repaired_dialogue = [
        _line("Old opener.", ["c1"]),
        _line("Repaired middle line.", ["c2"], speaker="B"),
        _line("Old ending.", ["c3"]),
    ]

    assert _changed_line_indexes(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
    ) == {1}
    assert _relevant_reaudit_line_indexes(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        original_issues=[{"line_index": 1, "scope": "repair", "action_required": True}],
        repair_mode="scene_repair",
        target_line_indexes=[],
    ) == [1]


def test_scene_repair_preserves_unflagged_sensitive_lines() -> None:
    original_dialogue = [
        _line("Original opener.", []),
        _line("Middle factual line.", ["c1"], speaker="B"),
        _line("Original ending.", []),
    ]
    repaired_dialogue = [
        _line("Different opener.", []),
        _line("Middle factual line.", ["c1"], speaker="B"),
        _line("Original ending.", []),
    ]

    assert _sensitive_line_indexes(original_dialogue) == {0, 1, 2}
    assert not _scene_repair_preserves_unflagged_sensitive_lines(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        actionable_line_indexes={1},
    )


def test_normalize_citation_issues_assigns_default_scope_from_severity() -> None:
    issues = _normalize_citation_issues(
        [
            {"line_index": 3, "type": "missing_claim_link", "severity": "high"},
            {"line_index": 4, "type": "audience_mismatch", "severity": "medium"},
            {"line_index": 5, "type": "forbidden_phrase", "severity": "low"},
        ]
    )

    assert issues == [
        {
            "line_index": 3,
            "type": "missing_claim_link",
            "severity": "high",
            "scope": "block",
            "line_function": "other",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 4,
            "type": "audience_mismatch",
            "severity": "medium",
            "scope": "repair",
            "line_function": "other",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 5,
            "type": "forbidden_phrase",
            "severity": "low",
            "scope": "record",
            "line_function": "other",
            "action_required": False,
            "description": "",
            "suggestion": "",
        },
    ]


def test_normalize_citation_issues_preserves_structured_line_function_signal() -> None:
    issues = _normalize_citation_issues(
        [
            {
                "line_index": 1,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "question_open",
                "action_required": False,
            }
        ]
    )

    assert issues == [
        {
            "line_index": 1,
            "type": "missing_claim_link",
            "severity": "high",
            "scope": "block",
            "line_function": "question_open",
            "action_required": False,
            "description": "",
            "suggestion": "",
        }
    ]


def test_citation_helpers_distinguish_repairable_and_blocking_issues() -> None:
    repairable = [{"type": "missing_attribution", "severity": "medium", "scope": "repair"}]
    blocking = [{"type": "missing_claim_link", "severity": "high", "scope": "block"}]
    recorded_high = [{"type": "audience_mismatch", "severity": "high", "scope": "record"}]
    informational_repair = [
        {
            "type": "missing_claim_link",
            "severity": "high",
            "scope": "repair",
            "action_required": False,
        }
    ]
    informational_block = [
        {
            "type": "missing_claim_link",
            "severity": "high",
            "scope": "block",
            "action_required": False,
        }
    ]

    assert _should_attempt_citation_repair(repairable)
    assert _should_attempt_citation_repair(blocking)
    assert not _should_attempt_citation_repair(informational_repair)
    assert not _has_blocking_citation_issues(repairable)
    assert not _has_blocking_citation_issues(recorded_high)
    assert _has_blocking_citation_issues(blocking)
    assert not _has_blocking_citation_issues(informational_block)


def test_post_filter_downgrades_only_short_pure_question_blockers() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("Что произошло?", ["c1"])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "question_open",
                "action_required": False,
                "description": "",
                "suggestion": "",
            },
            {
                "line_index": 0,
                "type": "unverified_stated_as_fact",
                "severity": "high",
                "scope": "block",
                "line_function": "question_open",
                "action_required": False,
                "description": "",
                "suggestion": "",
            },
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "low",
            "scope": "record",
            "line_function": "question_open",
            "action_required": False,
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 0,
            "type": "unverified_stated_as_fact",
            "severity": "low",
            "scope": "record",
            "line_function": "question_open",
            "action_required": False,
            "description": "",
            "suggestion": "",
        },
    ]


def test_post_filter_preserves_existing_record_for_english_pure_question() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("What happened?", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "low",
                "scope": "record",
                "line_function": "question_open",
                "action_required": False,
                "description": "",
                "suggestion": "No action required; questions do not require claim_ids.",
            }
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "low",
            "scope": "record",
            "line_function": "question_open",
            "action_required": False,
            "description": "",
            "suggestion": "No action required; questions do not require claim_ids.",
        }
    ]


def test_post_filter_downgrades_english_open_question_from_structured_signal() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("What happened?", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "question_open",
                "action_required": False,
                "description": "",
                "suggestion": "",
            }
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "low",
            "scope": "record",
            "line_function": "question_open",
            "action_required": False,
            "description": "",
            "suggestion": "",
        }
    ]


def test_post_filter_downgrades_transition_line_from_structured_signal() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("And then everything changed.", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "transition",
                "action_required": False,
                "description": "",
                "suggestion": "",
            }
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "low",
            "scope": "record",
            "line_function": "transition",
            "action_required": False,
            "description": "",
            "suggestion": "",
        }
    ]


def test_post_filter_keeps_actionable_transition_line_actionable() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("And then the government fixed the peg.", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "transition",
                "action_required": True,
                "description": "",
                "suggestion": "",
            }
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "medium",
            "scope": "repair",
            "line_function": "transition",
            "action_required": True,
            "description": "",
            "suggestion": "",
        }
    ]


def test_post_filter_keeps_loaded_wh_question_actionable() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("Почему частные банки вообще могли выпускать эти деньги?", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "question_with_premise",
                "action_required": True,
                "description": "",
                "suggestion": "",
            },
            {
                "line_index": 0,
                "type": "unverified_stated_as_fact",
                "severity": "high",
                "scope": "block",
                "line_function": "question_with_premise",
                "action_required": True,
                "description": "",
                "suggestion": "",
            },
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "medium",
            "scope": "repair",
            "line_function": "question_with_premise",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 0,
            "type": "unverified_stated_as_fact",
            "severity": "medium",
            "scope": "repair",
            "line_function": "question_with_premise",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
    ]


def test_post_filter_keeps_short_loaded_question_actionable() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("Кто это решил?", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "question_with_premise",
                "action_required": True,
                "description": "",
                "suggestion": "",
            },
            {
                "line_index": 0,
                "type": "unverified_stated_as_fact",
                "severity": "high",
                "scope": "block",
                "line_function": "question_with_premise",
                "action_required": True,
                "description": "",
                "suggestion": "",
            },
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "medium",
            "scope": "repair",
            "line_function": "question_with_premise",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 0,
            "type": "unverified_stated_as_fact",
            "severity": "medium",
            "scope": "repair",
            "line_function": "question_with_premise",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
    ]


def test_post_filter_keeps_recap_question_actionable() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("То есть именно частные банки и печатали эти деньги?", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "line_function": "question_with_premise",
                "action_required": True,
                "description": "",
                "suggestion": "",
            },
            {
                "line_index": 0,
                "type": "unverified_stated_as_fact",
                "severity": "high",
                "scope": "block",
                "line_function": "question_with_premise",
                "action_required": True,
                "description": "",
                "suggestion": "",
            },
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "medium",
            "scope": "repair",
            "line_function": "question_with_premise",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 0,
            "type": "unverified_stated_as_fact",
            "severity": "medium",
            "scope": "repair",
            "line_function": "question_with_premise",
            "action_required": True,
            "description": "",
            "suggestion": "",
        },
    ]


def test_post_filter_does_not_downgrade_factual_recap_line() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("То есть банк обещал обменивать банкноты на серебро.", [])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "description": "",
                "suggestion": "",
            }
        ],
    )

    assert issues == [
        {
            "line_index": 0,
            "type": "missing_claim_link",
            "severity": "high",
            "scope": "block",
            "description": "",
            "suggestion": "",
        }
    ]


def test_citation_repair_improved_accepts_better_post_repair_audit() -> None:
    original = {
        "passed": False,
        "issues": [{"line_index": 0, "severity": "high", "scope": "block"}],
        "metrics": {"fact_coverage_ratio": 0.6},
    }
    repaired = {
        "passed": False,
        "issues": [{"line_index": 0, "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.8},
    }

    assert _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Факт о механизме.", ["c1"])],
        repaired_dialogue=[_line("Факт о механизме теперь сформулирован точнее.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=4,
    )


def test_citation_repair_improved_rejects_blocker_fix_that_guts_fact_coverage() -> None:
    original = {
        "passed": False,
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "high", "scope": "block"}],
        "metrics": {"fact_coverage_ratio": 0.95},
    }
    repaired = {
        "passed": False,
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.05},
    }

    assert not _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Факт о механизме.", ["c1"])],
        repaired_dialogue=[_line("Слишком размыто.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=4,
    )


def test_citation_repair_improved_rejects_claim_loss_on_assertive_line_and_large_line_drift() -> None:
    original = {
        "passed": False,
        "issues": [{"severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.9},
    }
    repaired = {
        "passed": True,
        "issues": [],
        "metrics": {"fact_coverage_ratio": 0.95},
    }

    assert not _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Поддержанный факт.", ["c1", "c2"])],
        repaired_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=4,
    )
    assert not _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repaired_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=7,
    )


def test_citation_repair_improved_rejects_new_blockers_and_noop_non_blocking_repairs() -> None:
    original = {
        "passed": False,
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    repaired_with_new_blocker = {
        "passed": False,
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "high", "scope": "block"}],
        "metrics": {"fact_coverage_ratio": 0.91},
    }
    repaired_with_medium_noise = {
        "passed": False,
        "issues": [{"line_index": 2, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.89},
    }

    assert not _citation_repair_improved(
        original,
        repaired_with_new_blocker,
        original_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repaired_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=4,
    )
    assert not _citation_repair_improved(
        original,
        repaired_with_medium_noise,
        original_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repaired_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=4,
    )


def test_citation_repair_improved_accepts_non_blocking_severity_reduction() -> None:
    original = {
        "passed": False,
        "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    repaired = {
        "passed": False,
        "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "low", "scope": "record"}],
        "metrics": {"fact_coverage_ratio": 0.9},
    }

    assert _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repaired_dialogue=[_line("Поддержанный факт сформулирован мягче.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=4,
    )


def test_citation_repair_improved_ignores_new_medium_noise_on_untouched_lines() -> None:
    original_dialogue = [
        _line("Opener remains the same.", []),
        _line("Core factual line.", ["c1"], speaker="B"),
        _line("Untouched ending.", []),
    ]
    repaired_dialogue = [
        _line("Opener remains the same.", []),
        _line("Core factual line, now grounded.", ["c1", "c2"], speaker="B"),
        _line("Untouched ending.", []),
    ]
    original = {
        "passed": False,
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    repaired = {
        "passed": False,
        "issues": [{"line_index": 2, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }

    assert _citation_repair_improved(
        original,
        repaired,
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=3,
        repaired_line_count=3,
    )


def test_citation_repair_improved_rejects_scene_repair_that_changes_unflagged_sensitive_line() -> None:
    original_dialogue = [
        _line("Original opener.", []),
        _line("Flagged factual line.", ["c1"], speaker="B"),
        _line("Original landing.", []),
    ]
    repaired_dialogue = [
        _line("Different opener.", []),
        _line("Flagged factual line, repaired.", ["c1", "c2"], speaker="B"),
        _line("Original landing.", []),
    ]
    original = {
        "passed": False,
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    repaired = {
        "passed": False,
        "issues": [],
        "metrics": {"fact_coverage_ratio": 0.95},
    }

    assert not _citation_repair_improved(
        original,
        repaired,
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=3,
        repaired_line_count=3,
    )


def test_citation_repair_improved_allows_non_assertive_line_to_drop_claim_ids() -> None:
    original = {
        "passed": False,
        "issues": [{"line_index": 0, "type": "unverified_stated_as_fact", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    repaired = {
        "passed": True,
        "issues": [],
        "metrics": {"fact_coverage_ratio": 0.9},
    }

    assert _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Но что тогда мешало банку печатать сколько захочет?", ["c1"])],
        repaired_dialogue=[_line("Но какие механизмы все же ограничивали выпуск?", [])],
        repair_mode="line_repair",
        target_line_indexes=[0],
        original_line_count=1,
        repaired_line_count=1,
    )


def test_citation_repair_improved_requires_claim_ids_for_short_recap_line() -> None:
    original = {
        "passed": False,
        "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "high", "scope": "block"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    repaired = {
        "passed": False,
        "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }

    assert not _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("То есть банк обещал обменивать банкноты на серебро.", ["c1"])],
        repaired_dialogue=[_line("То есть банк обещал обменивать банкноты на серебро.", [])],
        repair_mode="line_repair",
        target_line_indexes=[0],
        original_line_count=1,
        repaired_line_count=1,
    )


def test_build_failed_citation_audit_creates_blocking_placeholder() -> None:
    audit = _build_failed_citation_audit(error="TimeoutError: request timed out")

    assert audit == {
        "passed": False,
        "issues": [
            {
                "line_index": -1,
                "type": "audit_failure",
                "severity": "high",
                "scope": "block",
                "description": "Citation audit could not complete, so the scene remains unaudited.",
                "suggestion": "TimeoutError: request timed out",
            }
        ],
        "metrics": {
            "fact_coverage_ratio": 0.0,
            "forbidden_phrase_hits": 0.0,
            "rigor_policy_violations": 0.0,
            "persona_consistency_score": 0.0,
            "temp_source_only_claims": 0.0,
        },
    }


@pytest.mark.asyncio
async def test_run_citation_audit_uses_haiku(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call_haiku_structured(**_kwargs):
        return """
        {
          "passed": false,
          "issues": [{"line_index": 0, "type": "missing_attribution", "severity": "medium", "scope": "repair"}],
          "metrics": {"fact_coverage_ratio": 0.75}
        }
        """

    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    audit = await _run_citation_audit(
        {"dialogue": [], "available_claims": {}, "forbidden_phrases": [], "rigor_policy": "", "language": "ru"}
    )

    assert audit == {
        "passed": False,
        "issues": [
            {
                "line_index": 0,
                "type": "missing_attribution",
                "severity": "medium",
                "scope": "repair",
                "line_function": "other",
                "action_required": True,
                "description": "",
                "suggestion": "",
            }
        ],
        "metrics": {
            "fact_coverage_ratio": 0.75,
            "forbidden_phrase_hits": 0.0,
            "rigor_policy_violations": 0.0,
            "persona_consistency_score": 0.0,
            "temp_source_only_claims": 0.0,
        },
    }


@pytest.mark.asyncio
async def test_run_citation_audit_downgrades_english_open_question_from_structured_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**_kwargs):
        return """
        {
          "passed": false,
          "issues": [
            {
              "line_index": 0,
              "type": "missing_claim_link",
              "severity": "high",
              "scope": "block",
              "line_function": "question_open",
              "action_required": false
            }
          ],
          "metrics": {"fact_coverage_ratio": 0.95}
        }
        """

    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    audit = await _run_citation_audit(
        {
            "dialogue": [_line("What happened?", [])],
            "available_claims": {},
            "forbidden_phrases": [],
            "rigor_policy": "",
            "language": "en",
        }
    )

    assert audit == {
        "passed": True,
        "issues": [
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "low",
                "scope": "record",
                "line_function": "question_open",
                "action_required": False,
                "description": "",
                "suggestion": "",
            }
        ],
        "metrics": {
            "fact_coverage_ratio": 0.95,
            "forbidden_phrase_hits": 0.0,
            "rigor_policy_violations": 0.0,
            "persona_consistency_score": 0.0,
            "temp_source_only_claims": 0.0,
        },
    }


@pytest.mark.asyncio
async def test_run_citation_audit_ignores_informational_transition_for_pass_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**_kwargs):
        return """
        {
          "passed": false,
          "issues": [
            {
              "line_index": 0,
              "type": "missing_claim_link",
              "severity": "high",
              "scope": "block",
              "line_function": "transition",
              "action_required": false
            }
          ],
          "metrics": {"fact_coverage_ratio": 0.95}
        }
        """

    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    audit = await _run_citation_audit(
        {
            "dialogue": [_line("And then everything changed.", [])],
            "available_claims": {},
            "forbidden_phrases": [],
            "rigor_policy": "",
            "language": "en",
        }
    )

    assert audit == {
        "passed": True,
        "issues": [
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "low",
                "scope": "record",
                "line_function": "transition",
                "action_required": False,
                "description": "",
                "suggestion": "",
            }
        ],
        "metrics": {
            "fact_coverage_ratio": 0.95,
            "forbidden_phrase_hits": 0.0,
            "rigor_policy_violations": 0.0,
            "persona_consistency_score": 0.0,
            "temp_source_only_claims": 0.0,
        },
    }


@pytest.mark.asyncio
async def test_run_citation_audit_keeps_actionable_transition_in_failure_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**_kwargs):
        return """
        {
          "passed": false,
          "issues": [
            {
              "line_index": 0,
              "type": "missing_claim_link",
              "severity": "high",
              "scope": "block",
              "line_function": "transition",
              "action_required": true
            }
          ],
          "metrics": {"fact_coverage_ratio": 0.95}
        }
        """

    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    audit = await _run_citation_audit(
        {
            "dialogue": [_line("And then the government fixed the peg.", [])],
            "available_claims": {},
            "forbidden_phrases": [],
            "rigor_policy": "",
            "language": "en",
        }
    )

    assert audit == {
        "passed": False,
        "issues": [
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "medium",
                "scope": "repair",
                "line_function": "transition",
                "action_required": True,
                "description": "",
                "suggestion": "",
            }
        ],
        "metrics": {
            "fact_coverage_ratio": 0.95,
            "forbidden_phrase_hits": 0.0,
            "rigor_policy_violations": 0.0,
            "persona_consistency_score": 0.0,
            "temp_source_only_claims": 0.0,
        },
    }


@pytest.mark.asyncio
async def test_run_citation_audit_with_retry_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    async def _flaky(_payload):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("first call timed out")
        return {"passed": True, "issues": [], "metrics": {"fact_coverage_ratio": 1.0}}

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._run_citation_audit", _flaky)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.settings.max_citation_audit_attempts", 2)

    audit = await _run_citation_audit_with_retry({"dialogue": []})

    assert calls["count"] == 2
    assert audit == {"passed": True, "issues": [], "metrics": {"fact_coverage_ratio": 1.0}}


@pytest.mark.asyncio
async def test_run_citation_audit_with_retry_propagates_stop_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    async def _stop(_payload):
        calls["count"] += 1
        raise StepExecutionStopped("stop requested")

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._run_citation_audit", _stop)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.settings.max_citation_audit_attempts", 2)

    with pytest.raises(StepExecutionStopped, match="stop requested"):
        await _run_citation_audit_with_retry({"dialogue": []})

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_attempt_citation_repair_retries_blocking_scene_once(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[dict] = []
    raw_responses = [
        {
            "repaired_lines": [
                {"line_index": 0, "speaker": "A", "text": "First attempt line?", "claim_ids": []},
            ]
        },
        {
            "repaired_lines": [
                {"line_index": 0, "speaker": "A", "text": "Second attempt line", "claim_ids": ["c1"]},
            ]
        },
    ]
    repaired_audits = [
        {
            "passed": False,
            "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "high", "scope": "block"}],
            "metrics": {"fact_coverage_ratio": 0.9},
        },
        {
            "passed": False,
            "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
            "metrics": {"fact_coverage_ratio": 0.92},
        },
    ]

    async def _fake_call_sonnet_structured(**kwargs):
        prompts.append(kwargs)
        return raw_responses[len(prompts) - 1]

    async def _fake_run_citation_audit_with_retry(_payload):
        return repaired_audits.pop(0)

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.call_sonnet_structured", _fake_call_sonnet_structured)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.parse_llm_json", lambda raw: raw)
    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor._run_citation_audit_with_retry",
        _fake_run_citation_audit_with_retry,
    )
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._dump_citation_repair_debug", lambda **_kwargs: None)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.settings.max_citation_repair_attempts", 2)

    repaired_dialogue, repaired_audit, accepted = await _attempt_citation_repair(
        run_id=str(uuid.uuid4()),
        segment=SimpleNamespace(
            act_number=3,
            scene_number=2,
            dialogue_json=[
                _line("Original line?", ["c1"]),
                _line("Follow-up", [], speaker="B"),
            ],
        ),
        claims_map={"c1": {"statement": "Claim"}},
        forbidden_phrases=[],
        rigor_policy="",
        language="ru",
        original_audit={
            "passed": False,
            "issues": [{"line_index": 0, "type": "unverified_stated_as_fact", "severity": "high", "scope": "block"}],
            "metrics": {"fact_coverage_ratio": 0.9},
        },
        scene_context={"scene_job": "cost", "scene_type": "debate", "description": "Test scene"},
    )

    assert accepted is True
    assert repaired_dialogue == [
        _line("Second attempt line", ["c1"]),
        _line("Follow-up", [], speaker="B"),
    ]
    assert repaired_audit == {
        "passed": False,
        "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    assert len(prompts) == 2
    assert '"repair_mode": "line_repair"' in prompts[0]["prompt"]
    assert '"repair_mode": "line_repair"' in prompts[1]["prompt"]
    assert "retry_feedback" in prompts[1]["prompt"]


@pytest.mark.asyncio
async def test_attempt_citation_repair_propagates_stop_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    async def _stop(**_kwargs):
        calls["count"] += 1
        raise StepExecutionStopped("stop requested")

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.call_sonnet_structured", _stop)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._dump_citation_repair_debug", lambda **_kwargs: None)

    with pytest.raises(StepExecutionStopped, match="stop requested"):
        await _attempt_citation_repair(
            run_id=str(uuid.uuid4()),
            segment=SimpleNamespace(
                act_number=3,
                scene_number=2,
                dialogue_json=[
                    _line("Original line?", ["c1"]),
                    _line("Follow-up", [], speaker="B"),
                ],
            ),
            claims_map={"c1": {"statement": "Claim"}},
            forbidden_phrases=[],
            rigor_policy="",
            language="ru",
            original_audit={
                "passed": False,
                "issues": [{"line_index": 0, "type": "unverified_stated_as_fact", "severity": "high", "scope": "block"}],
                "metrics": {"fact_coverage_ratio": 0.9},
            },
            scene_context={"scene_job": "cost", "scene_type": "debate", "description": "Test scene"},
        )

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_attempt_citation_repair_line_mode_splices_only_targeted_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call_sonnet_structured(**_kwargs):
        return {
            "repaired_lines": [
                {"line_index": 1, "speaker": "B", "text": "Тогда другой вопрос: что их ограничивало?", "claim_ids": []}
            ]
        }

    async def _fake_run_citation_audit_with_retry(_payload):
        return {"passed": True, "issues": [], "metrics": {"fact_coverage_ratio": 0.95}}

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.call_sonnet_structured", _fake_call_sonnet_structured)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.parse_llm_json", lambda raw: raw)
    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor._run_citation_audit_with_retry",
        _fake_run_citation_audit_with_retry,
    )
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._dump_citation_repair_debug", lambda **_kwargs: None)

    repaired_dialogue, repaired_audit, accepted = await _attempt_citation_repair(
        run_id=str(uuid.uuid4()),
        segment=SimpleNamespace(
            act_number=1,
            scene_number=6,
            dialogue_json=[
                _line("Линия один.", ["c1"]),
                _line("Но кто тогда мешал печатать сколько угодно?", ["c2"], speaker="B"),
                _line("Линия три.", ["c3"]),
            ],
        ),
        claims_map={"c1": {"statement": "Claim 1"}, "c2": {"statement": "Claim 2"}, "c3": {"statement": "Claim 3"}},
        forbidden_phrases=[],
        rigor_policy="",
        language="ru",
        original_audit={
            "passed": False,
            "issues": [{"line_index": 1, "type": "unverified_stated_as_fact", "severity": "medium", "scope": "repair"}],
            "metrics": {"fact_coverage_ratio": 0.93},
        },
        scene_context={"scene_job": "synthesize", "scene_type": "takeaway", "description": "Closing scene"},
    )

    assert accepted is True
    assert repaired_audit == {"passed": True, "issues": [], "metrics": {"fact_coverage_ratio": 0.95}}
    assert repaired_dialogue == [
        _line("Линия один.", ["c1"]),
        _line("Тогда другой вопрос: что их ограничивало?", [], speaker="B"),
        _line("Линия три.", ["c3"]),
    ]


class _FakeResult:
    def __init__(self, scalar) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeCitationDB:
    def __init__(self, segments) -> None:
        self._segments = list(segments)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _statement):
        return _FakeResult(self._segments.pop(0))

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_audit_citations_fails_closed_when_audit_call_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    segment = SimpleNamespace(
        id=uuid.uuid4(),
        act_number=2,
        scene_number=3,
        dialogue_json=[_line("Line", ["c1"])],
        quality_metrics=None,
        audit_findings=None,
        audit_status=AuditStatus.PENDING,
    )
    db = _FakeCitationDB([segment])

    async def _boom(_payload):
        raise TimeoutError("request timed out")

    async def _fake_load_claims_map(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.async_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._run_citation_audit", _boom)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._load_claims_map", _fake_load_claims_map)

    with pytest.raises(RuntimeError, match="Citation audit blocked audio"):
        await audit_citations(
            {
                "generation_run_id": str(uuid.uuid4()),
                "script_segment_ids": [str(segment.id)],
                "quality_signals": {},
                "language": "ru",
                "claim_ids": [],
            }
        )

    assert db.committed is True
    assert segment.audit_status == AuditStatus.FAILED
    assert segment.quality_metrics == {
        "fact_coverage_ratio": 0.0,
        "forbidden_phrase_hits": 0.0,
        "rigor_policy_violations": 0.0,
        "persona_consistency_score": 0.0,
        "temp_source_only_claims": 0.0,
    }
    assert segment.audit_findings == {
        "citation": {
            "passed": False,
            "issues": [
                {
                    "line_index": -1,
                    "type": "audit_failure",
                    "severity": "high",
                    "scope": "block",
                    "description": "Citation audit could not complete, so the scene remains unaudited.",
                    "suggestion": "TimeoutError: request timed out",
                }
            ],
            "metrics": {
                "fact_coverage_ratio": 0.0,
                "forbidden_phrase_hits": 0.0,
                "rigor_policy_violations": 0.0,
                "persona_consistency_score": 0.0,
                "temp_source_only_claims": 0.0,
            },
            "repair_attempted": False,
            "repair_accepted": False,
            "blocking": True,
        }
    }


@pytest.mark.asyncio
async def test_audit_citations_propagates_stop_signal_without_marking_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    segment = SimpleNamespace(
        id=uuid.uuid4(),
        act_number=2,
        scene_number=3,
        dialogue_json=[_line("Line", ["c1"])],
        quality_metrics=None,
        audit_findings=None,
        audit_status=AuditStatus.PENDING,
    )
    db = _FakeCitationDB([segment])

    async def _stop(_payload):
        raise StepExecutionStopped("stop requested")

    async def _fake_load_claims_map(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.async_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._run_citation_audit_with_retry", _stop)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._load_claims_map", _fake_load_claims_map)

    with pytest.raises(StepExecutionStopped, match="stop requested"):
        await audit_citations(
            {
                "generation_run_id": str(uuid.uuid4()),
                "script_segment_ids": [str(segment.id)],
                "quality_signals": {},
                "language": "ru",
                "claim_ids": [],
            }
        )

    assert db.committed is False
    assert segment.audit_status == AuditStatus.PENDING
    assert segment.quality_metrics is None
    assert segment.audit_findings is None


@pytest.mark.asyncio
async def test_audit_citations_stops_after_first_unresolved_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    first_segment = SimpleNamespace(
        id=uuid.uuid4(),
        act_number=1,
        scene_number=1,
        dialogue_json=[_line("Но кто мешал банку печатать сколько угодно?", ["c1"])],
        quality_metrics=None,
        audit_findings=None,
        audit_status=AuditStatus.PENDING,
    )
    second_segment = SimpleNamespace(
        id=uuid.uuid4(),
        act_number=1,
        scene_number=2,
        dialogue_json=[_line("Вторая сцена.", ["c2"])],
        quality_metrics=None,
        audit_findings=None,
        audit_status=AuditStatus.PENDING,
    )
    db = _FakeCitationDB([first_segment, second_segment])
    audit_calls = {"count": 0}

    async def _fake_run_citation_audit_with_retry(_payload):
        audit_calls["count"] += 1
        return {
            "passed": False,
            "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "high", "scope": "block"}],
            "metrics": {"fact_coverage_ratio": 0.8},
        }

    async def _fake_attempt_citation_repair(**_kwargs):
        return None, None, False

    async def _fake_load_claims_map(*_args, **_kwargs):
        return {"c1": {"statement": "Claim 1"}, "c2": {"statement": "Claim 2"}}

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor._run_citation_audit_with_retry",
        _fake_run_citation_audit_with_retry,
    )
    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor._attempt_citation_repair",
        _fake_attempt_citation_repair,
    )
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._load_claims_map", _fake_load_claims_map)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.publish_event", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Citation audit blocked audio"):
        await audit_citations(
            {
                "generation_run_id": str(uuid.uuid4()),
                "script_segment_ids": [str(first_segment.id), str(second_segment.id)],
                "quality_signals": {},
                "language": "ru",
                "claim_ids": [],
            }
        )

    assert audit_calls["count"] == 1
    assert db.committed is True
    assert first_segment.audit_status == AuditStatus.FAILED
    assert second_segment.audit_status == AuditStatus.PENDING
