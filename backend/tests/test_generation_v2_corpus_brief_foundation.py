from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.generation_v2 import corpus_brief_persistence
from app.generation_v2.corpus_brief_preview import corpus_brief_preview
from app.generation_v2.contracts.source import (
    CorpusBriefAuditIssueCode,
    CorpusBriefAuditSeverity,
    CoverageOutcomeStatus,
    CoverageQuestionImportance,
    CoverageQuestionKind,
    SourceContributionUniqueness,
    SourceDisposition,
    SourceEvidenceAuthority,
    SourceTopicRelevance,
)
from app.generation_v2.schemas_source import (
    CorpusBrief,
    CorpusBriefAuditIssue,
    CorpusBriefAuditReport,
    CorpusBriefIntent,
    CorpusBriefSourceSet,
    CoverageOutcome,
    CoverageQuestion,
    ResearchHandoffItem,
    SourceAssessment,
    SourceGapRecord,
    SourceMap,
)
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref
from app.generation_v2.source_profiles import (
    legacy_source_profile,
    resolve_source_profile,
    save_source_profile,
    source_profile_fingerprint,
)
from app.models.generation_v2 import ProjectSourceProfile, SourceVersion, V2ArtifactType


def _source_version(*, content_hash: str = "a" * 64, metadata: dict | None = None) -> SourceVersion:
    return SourceVersion(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        kind="web",
        storage_path="sources/example.html",
        content_hash=content_hash,
        metadata_=metadata or {},
    )


def _brief(*, long_text: str = "") -> CorpusBrief:
    question = CoverageQuestion(
        question_id="q_origin",
        question="Why did the system emerge?",
        kind=CoverageQuestionKind.ORIGIN,
        importance=CoverageQuestionImportance.CRITICAL,
        rationale=long_text,
    )
    return CorpusBrief(
        generation_run_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        run_corpus_view_id=str(uuid.uuid4()),
        corpus_revision=2,
        attempt=3,
        source_map_artifact_id=str(uuid.uuid4()),
        source_map_hash="b" * 64,
        intent=CorpusBriefIntent(user_prompt=long_text or "Explain the system"),
        coverage_questions=[question],
        source_sets=[
            CorpusBriefSourceSet(
                set_id="set_history",
                label="History",
                purpose=long_text or "Map the origin",
                source_ids=["source-1"],
                question_ids=[question.question_id],
            )
        ],
        coverage_outcomes=[
            CoverageOutcome(
                outcome_id="outcome_origin",
                question_id=question.question_id,
                status=CoverageOutcomeStatus.PARTIAL,
                source_ids=["source-1"],
                evidence_ids=["evidence-1"],
                orientation_summary=long_text,
            )
        ],
        research_handoff=[
            ResearchHandoffItem(
                handoff_id="handoff_origin",
                objective=long_text or "Deep-read the legal history",
                question_ids=[question.question_id],
                preferred_source_ids=["source-1"],
                starting_evidence_ids=["evidence-1"],
            )
        ],
        summary=long_text,
    )


def test_legacy_source_map_remains_readable() -> None:
    source_map = SourceMap.model_validate(
        {
            "source_cards": [],
            "quote_cards": [],
            "source_triage": {"ranked_sources": []},
            "gaps": ["legacy gap"],
        }
    )

    assert source_map.schema_version == "source_map.v1"
    assert source_map.source_assessments == []
    assert source_map.gaps == ["legacy gap"]


def test_source_map_v2_keeps_three_independent_assessment_axes() -> None:
    assessment = SourceAssessment(
        assessment_id="assessment-1",
        source_id="source-1",
        relevance=SourceTopicRelevance.CORE,
        relevance_reason="Directly addresses the user question.",
        authority=SourceEvidenceAuthority.SECONDARY,
        authority_reason="A well-sourced scholarly review, not a primary record.",
        uniqueness=SourceContributionUniqueness.UNIQUE,
        uniqueness_reason="Only source that covers the institutional transition.",
        disposition=SourceDisposition.ANCHOR,
        coverage_question_ids=["q_origin"],
    )
    source_map = SourceMap(schema_version="source_map.v2", source_assessments=[assessment])

    stored = source_map.model_dump(mode="json")["source_assessments"][0]
    assert stored["relevance"] == "core"
    assert stored["authority"] == "secondary"
    assert stored["uniqueness"] == "unique"


def test_corpus_brief_preserves_long_semantic_fields_without_truncation() -> None:
    long_text = "evidence-backed orientation " * 1_000
    brief = _brief(long_text=long_text)

    payload = brief.model_dump(mode="json")
    restored = CorpusBrief.model_validate(payload)

    assert restored.summary == long_text
    assert restored.coverage_questions[0].rationale == long_text
    assert restored.coverage_outcomes[0].orientation_summary == long_text
    assert restored.research_handoff[0].objective == long_text

    preview = corpus_brief_preview(restored, text_limit=120)
    assert preview["summary"] == {
        "preview": long_text[:120],
        "chars": len(long_text),
        "truncated": True,
    }
    assert restored.summary == long_text


def test_corpus_brief_gap_preview_uses_stable_public_reference() -> None:
    first_gap = SourceGapRecord(
        gap_id="gap:first",
        kind="coverage",
        description="An earlier gap.",
    )
    stable_gap = SourceGapRecord(
        gap_id="gap:stable-id",
        kind="contradiction",
        description="Two accounts remain unresolved.",
        source_ids=["source-1"],
    )
    brief = _brief().model_copy(
        update={"gap_records": [first_gap, stable_gap]}
    )

    before = corpus_brief_preview(brief)
    after = corpus_brief_preview(brief.model_copy(update={"gap_records": [stable_gap]}))

    stable_ref = public_gap_ref("gap:stable-id")
    assert before["gap_records"][1]["gap_ref"] == stable_ref
    assert after["gap_records"][0]["gap_ref"] == stable_ref


def test_corpus_brief_rejects_unknown_question_reference() -> None:
    brief = _brief().model_dump(mode="json")
    brief["source_sets"][0]["question_ids"] = ["q_missing"]

    with pytest.raises(ValidationError, match="unknown coverage question ids"):
        CorpusBrief.model_validate(brief)


def test_corpus_brief_audit_backend_result_fails_with_blocking_issue() -> None:
    issue = CorpusBriefAuditIssue(
        issue_id="issue-1",
        code=CorpusBriefAuditIssueCode.UNSUPPORTED_COVERAGE,
        severity=CorpusBriefAuditSeverity.BLOCKING,
        message="Covered outcome has no support.",
    )

    report = CorpusBriefAuditReport(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        source_map_artifact_id=str(uuid.uuid4()),
        passed=True,
        summary="Contradictory audit",
        issues=[issue],
    )

    assert report.reported_passed is True
    assert report.passed is False


def test_legacy_profile_adapter_preserves_source_metadata_without_capsule_feedback() -> None:
    long_summary = "full semantic source summary " * 1_000
    version = _source_version(
        metadata={
            "neutral_summary": long_summary,
            "source_type": "official",
            "outline_hints": ["Metadata introduction", "Metadata mechanism"],
            "readability_warnings": ["Metadata table warning"],
            "v2_triage_capsule": {
                "body": {
                    "outline": ["Agent-generated introduction", "Agent-generated mechanism"],
                    "readability_warnings": ["Agent-generated table warning"],
                }
            },
        }
    )

    profile = legacy_source_profile(version)

    assert profile.neutral_summary == long_summary
    assert profile.outline_hints == ["Metadata introduction", "Metadata mechanism"]
    assert profile.readability_warnings == ["Metadata table warning"]
    assert "Agent-generated introduction" not in profile.outline_hints
    assert "Agent-generated table warning" not in profile.readability_warnings
    assert profile.profile_origin == "legacy_adapter"


def test_source_profile_fingerprint_is_version_scoped_and_stable() -> None:
    version = _source_version()
    first = source_profile_fingerprint(version)
    second = source_profile_fingerprint(version)
    version.content_hash = "c" * 64

    assert first == second
    assert source_profile_fingerprint(version) != first


def test_legacy_profile_does_not_promote_representation_kind_to_source_kind() -> None:
    version = _source_version(metadata={"source_type": "extracted_text"})
    version.kind = "extracted_text"

    profile = legacy_source_profile(version)

    assert profile.source_kind == "unknown"
    assert profile.representation_preference == "extracted_text"


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar(self):
        return self.value


class _FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.flush_count = 0

    async def execute(self, _query):
        return _ScalarResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


@pytest.mark.asyncio
async def test_profile_resolution_falls_back_without_persisting() -> None:
    version = _source_version(metadata={"neutral_summary": "Legacy orientation"})
    db = _FakeSession([None])

    profile = await resolve_source_profile(db, version)

    assert profile.neutral_summary == "Legacy orientation"
    assert profile.profile_origin == "legacy_adapter"
    assert db.added == []


@pytest.mark.asyncio
async def test_source_profile_persistence_versions_current_record() -> None:
    version = _source_version()
    previous_profile = legacy_source_profile(version)
    previous_profile.neutral_summary = "Old summary"
    previous = ProjectSourceProfile(
        id=uuid.uuid4(),
        project_id=version.project_id,
        source_id=version.source_id,
        source_version_id=version.id,
        schema_version=previous_profile.schema_version,
        version=1,
        is_current=True,
        source_fingerprint=source_profile_fingerprint(version),
        payload=previous_profile.model_dump(mode="json"),
        status="ready",
    )
    updated = legacy_source_profile(version)
    updated.neutral_summary = "Updated summary"
    db = _FakeSession([None, previous, 1])

    record = await save_source_profile(db, source_version=version, profile=updated)

    assert previous.is_current is False
    assert record.version == 2
    assert record.supersedes_id == previous.id
    assert record.payload["neutral_summary"] == "Updated summary"
    assert db.added == [record]
    assert db.flush_count == 2


@pytest.mark.asyncio
async def test_source_profile_persistence_reuses_identical_current_record() -> None:
    version = _source_version()
    profile = legacy_source_profile(version)
    current = ProjectSourceProfile(
        id=uuid.uuid4(),
        project_id=version.project_id,
        source_id=version.source_id,
        source_version_id=version.id,
        schema_version=profile.schema_version,
        version=1,
        is_current=True,
        source_fingerprint=source_profile_fingerprint(version),
        payload=profile.model_dump(mode="json"),
        status="ready",
    )
    db = _FakeSession([None, current])

    record = await save_source_profile(db, source_version=version, profile=profile)

    assert record is current
    assert db.added == []
    assert db.flush_count == 0


@pytest.mark.asyncio
async def test_corpus_brief_persistence_pins_run_scope(monkeypatch) -> None:
    brief = _brief()
    captured = {}

    async def fake_save_artifact(_db, **kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(corpus_brief_persistence, "save_artifact", fake_save_artifact)

    await corpus_brief_persistence.save_corpus_brief(
        object(),
        generation_run_id=uuid.UUID(brief.generation_run_id),
        brief=brief,
    )

    assert captured["artifact_type"] == V2ArtifactType.CORPUS_BRIEF
    assert captured["corpus_revision"] == brief.corpus_revision
    assert captured["attempt"] == brief.attempt
    assert captured["input_artifact_ids"] == [brief.source_map_artifact_id]
