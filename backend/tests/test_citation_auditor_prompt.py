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
    _build_failed_citation_audit,
    _citation_repair_improved,
    _has_blocking_citation_issues,
    _load_claims_map,
    _normalize_citation_issues,
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
    assert "must be repaired or block audio" in AUDITOR_SYSTEM
    assert "Questions, callbacks, recaps, scene transitions, and synthesis lines" in AUDITOR_SYSTEM
    assert "rewrite only the targeted lines" in LINE_REPAIR_SYSTEM.lower()
    assert "Fix only the citation and rigor issues that were flagged." in REPAIR_SYSTEM
    assert "Do not add new facts, institutions, dates, figures, or claims." in REPAIR_SYSTEM
    assert "Preserve the scene's `scene_job`, `scene_type`, and content function." in REPAIR_SYSTEM


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
    )

    claims_map = await _load_claims_map(_FakeClaimsDB([claim]), [str(claim.id)])

    assert claims_map == {
        str(claim.id): {
            "type": "fact",
            "verification": "verified",
            "statement": claim.statement,
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
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 4,
            "type": "audience_mismatch",
            "severity": "medium",
            "scope": "repair",
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 5,
            "type": "forbidden_phrase",
            "severity": "low",
            "scope": "record",
            "description": "",
            "suggestion": "",
        },
    ]


def test_citation_helpers_distinguish_repairable_and_blocking_issues() -> None:
    repairable = [{"type": "missing_attribution", "severity": "medium", "scope": "repair"}]
    blocking = [{"type": "missing_claim_link", "severity": "high", "scope": "block"}]
    recorded_high = [{"type": "audience_mismatch", "severity": "high", "scope": "record"}]

    assert _should_attempt_citation_repair(repairable)
    assert _should_attempt_citation_repair(blocking)
    assert not _has_blocking_citation_issues(repairable)
    assert not _has_blocking_citation_issues(recorded_high)
    assert _has_blocking_citation_issues(blocking)


def test_post_filter_downgrades_non_assertive_question_blockers() -> None:
    issues = _post_filter_citation_issues(
        dialogue=[_line("Но что тогда мешало банку печатать сколько захочет?", ["c1"])],
        issues=[
            {
                "line_index": 0,
                "type": "missing_claim_link",
                "severity": "high",
                "scope": "block",
                "description": "",
                "suggestion": "",
            },
            {
                "line_index": 0,
                "type": "unverified_stated_as_fact",
                "severity": "high",
                "scope": "block",
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
            "description": "",
            "suggestion": "",
        },
        {
            "line_index": 0,
            "type": "unverified_stated_as_fact",
            "severity": "medium",
            "scope": "repair",
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
        "issues": [{"severity": "high", "scope": "block"}],
        "metrics": {"fact_coverage_ratio": 0.6},
    }
    repaired = {
        "passed": False,
        "issues": [{"severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.8},
    }

    assert _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Факт о механизме.", ["c1"])],
        repaired_dialogue=[_line("Факт о механизме.", ["c1"])],
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
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    repaired = {
        "passed": False,
        "issues": [{"line_index": 1, "type": "missing_claim_link", "severity": "low", "scope": "record"}],
        "metrics": {"fact_coverage_ratio": 0.9},
    }

    assert _citation_repair_improved(
        original,
        repaired,
        original_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repaired_dialogue=[_line("Поддержанный факт.", ["c1"])],
        repair_mode="scene_repair",
        target_line_indexes=[],
        original_line_count=4,
        repaired_line_count=4,
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
            "dialogue": [
                _line("Second attempt line", ["c1"]),
                _line("Follow-up", [], speaker="B"),
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
    assert repaired_dialogue == raw_responses[1]["dialogue"]
    assert repaired_audit == {
        "passed": False,
        "issues": [{"line_index": 0, "type": "missing_claim_link", "severity": "medium", "scope": "repair"}],
        "metrics": {"fact_coverage_ratio": 0.92},
    }
    assert len(prompts) == 2
    assert '"repair_mode": "line_repair"' in prompts[0]["prompt"]
    assert '"repair_mode": "scene_repair"' in prompts[1]["prompt"]
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
