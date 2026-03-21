import uuid
from types import SimpleNamespace

import pytest

from app.models.script_segment import AuditStatus
from app.pipeline.phase6_writing.citation_auditor import (
    AUDITOR_SYSTEM,
    REPAIR_SYSTEM,
    _build_failed_citation_audit,
    _build_citation_audit_prompt_payload,
    _citation_repair_improved,
    audit_citations,
    _run_citation_audit,
    _has_blocking_citation_issues,
    _normalize_citation_issues,
    _should_attempt_citation_repair,
)


def test_citation_auditor_prompt_demands_severity_scope_and_repair() -> None:
    assert '"severity": "low|medium|high"' in AUDITOR_SYSTEM
    assert '"scope": "record|repair|block"' in AUDITOR_SYSTEM
    assert "must be repaired or block audio" in AUDITOR_SYSTEM
    assert "Fix only the citation and rigor issues that were flagged." in REPAIR_SYSTEM
    assert "Do not add new facts, institutions, dates, figures, or claims." in REPAIR_SYSTEM


def test_build_citation_audit_prompt_payload_keeps_policy_and_claim_context() -> None:
    payload = _build_citation_audit_prompt_payload(
        dialogue=[{"speaker": "A", "text": "Line", "claim_ids": ["c1"]}],
        claims_map={"c1": {"type": "fact", "verification": "verified", "statement": "Claim"}},
        forbidden_phrases=["game changer"],
        rigor_policy="No unsupported certainty.",
        language="ru",
    )

    assert payload == {
        "dialogue": [{"speaker": "A", "text": "Line", "claim_ids": ["c1"]}],
        "available_claims": {"c1": {"type": "fact", "verification": "verified", "statement": "Claim"}},
        "forbidden_phrases": ["game changer"],
        "rigor_policy": "No unsupported certainty.",
        "language": "ru",
    }


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

    assert _citation_repair_improved(original, repaired)


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

    audit = await _run_citation_audit({"dialogue": [], "available_claims": {}, "forbidden_phrases": [], "rigor_policy": "", "language": "ru"})

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


class _FakeResult:
    def __init__(self, scalar) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeCitationDB:
    def __init__(self, segment) -> None:
        self.segment = segment
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _statement):
        return _FakeResult(self.segment)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_audit_citations_fails_closed_when_audit_call_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    segment = SimpleNamespace(
        id=uuid.uuid4(),
        act_number=2,
        scene_number=3,
        dialogue_json=[{"speaker": "A", "text": "Line", "claim_ids": ["c1"]}],
        quality_metrics=None,
        audit_findings=None,
        audit_status=AuditStatus.PENDING,
    )
    db = _FakeCitationDB(segment)

    async def _boom(_payload):
        raise TimeoutError("request timed out")

    async def _fake_load_claims_map(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.async_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor._run_citation_audit", _boom)
    monkeypatch.setattr("app.pipeline.phase6_writing.citation_auditor.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.pipeline.phase6_writing.citation_auditor._load_claims_map",
        _fake_load_claims_map,
    )

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
