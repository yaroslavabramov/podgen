from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.generation_v2.runtime_tool_output import forbidden_tool_output_keys, model_facing_tool_output
from app.generation_v2.agents.source_acquisition_agent import _model_gap_report, _model_source_inventory
from app.generation_v2.pipeline.source_acquisition_flow import (
    _attach_acquired_sources_to_explicit_selection,
    persisted_source_acquisition_rounds_used,
)
from app.generation_v2.pipeline.source_triage_gate_repair_flow import source_triage_repair_already_used
from app.generation_v2.safe_browser import guarded_page_goto, public_browser_launch_args
from app.generation_v2.schemas import (
    CorpusGapReport,
    ResearchAudit,
    ResearchAuditIssue,
    SourceAcquisitionCandidate,
    SourceCard,
    SourceMap,
)
from app.generation_v2.source_acquisition import remaining_acquisition_rounds
from app.generation_v2.source_acquisition_gaps import build_corpus_gap_report
from app.generation_v2.source_acquisition_ingest import native_first_ingest_candidate
from app.generation_v2.source_acquisition_limits import existing_source_for_url, validation_rejection
from app.generation_v2.source_acquisition_toolset import SourceAcquisitionToolset
from app.generation_v2.source_acquisition_validation import title_from_url, validate_candidate_static
from app.generation_v2.source_acquisition_validation import validate_source_acquisition_tool_args
from app.generation_v2.tools.evidence_model_read_utils import (
    model_file_read_prompt,
    parse_model_file_read_result,
)
from app.generation_v2.source_corpus_provider_detach import (
    detach_provider_file_from_active_index,
    retry_pending_provider_detaches,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context
from app.generation_v2.tools.evidence_write_validation import validate_source_map_write_args
from app.ingestion.router import IngestionIndexingError, _validated_source_title_for_persistence
from app.ingestion.canonicalizer import _DATE_EXTRACTION_SYSTEM
from app.ingestion.source_metadata import _AUTHORITY_SYSTEM
from app.ingestion.web_quality import resolve_web_title
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, V2ArtifactType
from app.models.source import IngestionStatus, Source, SourceType


def _ready_sync_summary() -> dict[str, object]:
    return {
        "canonical_source_version_id": "version-1",
        "provider_vector_store_id": "vs-1",
        "ready_provider_file_count": 1,
    }


def test_acquired_source_joins_an_explicit_run_selection_before_retriage() -> None:
    run = SimpleNamespace(config={"source_ids": ["source-original"], "language": "ru"})

    _attach_acquired_sources_to_explicit_selection(
        run,  # type: ignore[arg-type]
        ["source-repair", "source-original"],
    )

    assert run.config == {
        "source_ids": ["source-original", "source-repair"],
        "language": "ru",
    }


def test_acquired_source_does_not_pin_an_implicit_all_sources_run() -> None:
    run = SimpleNamespace(config={"language": "ru"})

    _attach_acquired_sources_to_explicit_selection(run, ["source-repair"])  # type: ignore[arg-type]

    assert run.config == {"language": "ru"}


def _gap_report() -> CorpusGapReport:
    return CorpusGapReport(
        run_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        trigger_phase="source_triage",
        target_duration_seconds=1800,
        summary="Need a source",
        gaps=[
            {
                "gap_id": "gap-1",
                "kind": "missing_function",
                "description": "Need mechanism coverage",
            }
        ],
    )


def _source(*, temporary: bool = True, status: IngestionStatus = IngestionStatus.PENDING) -> Source:
    return Source(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        original_url="https://example.com/report",
        title="Report",
        ingestion_status=status,
        is_temporary=temporary,
        fully_ingested=False,
        metadata_={},
    )


def test_nested_reader_prompts_treat_source_content_as_untrusted_and_hide_internal_ids() -> None:
    prompt = model_file_read_prompt(
        focus_question="What does the source support?",
        claim_text="A candidate claim.",
        purpose="research",
        locator_hint="section 2",
        source_kind="extracted_text",
    )

    assert "untrusted data" in prompt
    assert "source_id:" not in prompt
    assert "source_version_id:" not in prompt
    assert "untrusted data" in _AUTHORITY_SYSTEM
    assert "untrusted data" in _DATE_EXTRACTION_SYSTEM


def test_model_file_read_parser_rejects_incomplete_or_contradictory_payloads() -> None:
    valid = {
        "supports_query": True,
        "support_level": "supported",
        "quote_text": "",
        "snippet_text": "A grounded snippet.",
        "support_summary": "The source supports the question.",
        "uncertainty": "",
        "locator_text": "section 2",
        "locator_type": "section",
        "confidence": 0.8,
    }

    assert parse_model_file_read_result(json.dumps(valid)) == valid
    with pytest.raises(RuntimeError, match="invalid structured payload"):
        parse_model_file_read_result(json.dumps({key: value for key, value in valid.items() if key != "support_summary"}))
    with pytest.raises(RuntimeError, match="supports_query contradicts support_level"):
        parse_model_file_read_result(json.dumps({**valid, "supports_query": False}))
    with pytest.raises(RuntimeError, match="valid boolean"):
        parse_model_file_read_result(json.dumps({**valid, "supports_query": "false"}))
    with pytest.raises(RuntimeError, match="less than or equal to 1"):
        parse_model_file_read_result(json.dumps({**valid, "confidence": 1.5}))


def test_acquisition_title_is_rejected_instead_of_truncated() -> None:
    with pytest.raises(ValueError, match="semantic_field_too_long: field=source.title"):
        title_from_url("https://example.com/" + ("a" * 501))

    with pytest.raises(ValueError, match="semantic_field_too_long: field=source.title"):
        _validated_source_title_for_persistence("a" * 501)

    resolved = resolve_web_title("A" * 501, text="Article body", url="https://example.com/article")
    assert resolved == "A" * 501
    with pytest.raises(ValueError, match="semantic_field_too_long: field=source.title"):
        _validated_source_title_for_persistence(resolved)


def test_acquisition_url_is_rejected_instead_of_left_to_database_truncation() -> None:
    url = "https://example.com/" + ("a" * 2100)
    candidate = SourceAcquisitionCandidate(
        url=url,
        normalized_url=url,
        gap_ids=["gap-1"],
        rationale="Primary report",
        confidence=0.9,
    )

    assert validate_candidate_static(candidate, _gap_report()) == (
        f"semantic_field_too_long: field=source.original_url max_length=2048 actual_length={len(url)}"
    )


def test_source_acquisition_prompt_payload_hides_internal_ids() -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[
            SourceCard(
                source_id=source_id,
                title="Current source",
                source_type="web",
                evidence_ids=[evidence_id],
                summary=f"Existing summary mentions evidence {evidence_id}.",
                gaps_or_cautions=[f"Caution references source {source_id}."],
            )
        ]
    )
    context = build_source_ref_context(inventory)
    report = CorpusGapReport(
        run_id=run_id,
        project_id=project_id,
        trigger_phase="source_triage",
        target_duration_seconds=1800,
        summary="Need coverage",
        gaps=[
            {
                "gap_id": "gap-1",
                "kind": "coverage",
                "description": f"Source {source_id} lacks support; invalid evidence {evidence_id}.",
                "related_source_ids": [source_id],
            }
        ],
    )

    model_payload = _model_gap_report(report, context)
    model_inventory = _model_source_inventory(inventory, context)
    serialized = json.dumps({"report": model_payload, "inventory": model_inventory})

    assert run_id not in serialized
    assert project_id not in serialized
    assert source_id not in serialized
    assert evidence_id not in serialized
    assert "S01" in serialized
    assert "related_source_ids" not in serialized


def test_oversized_model_write_fields_are_rejected_without_truncation() -> None:
    source_map_error = validate_source_map_write_args(
        "upsert_source_card",
        {
            "source_ref": "S01",
            "summary": "x" * 5_001,
            "useful_for": [],
            "evidence_ids": [],
            "gaps_or_cautions": [],
        },
    )
    acquisition_error = validate_source_acquisition_tool_args(
        "ingest_source_candidate",
        {
            "url": "https://example.com/report",
            "gap_ids": ["gap-1"],
            "expected_editorial_function": "mechanism",
            "rationale": "x" * 4_001,
            "source_type_hint": "web",
            "authority_hint": "primary",
        },
    )

    assert source_map_error == {
        "accepted": False,
        "error": "semantic_field_too_long: field=summary max_length=5000 actual_length=5001",
    }
    assert acquisition_error == {
        "accepted": False,
        "error": "semantic_field_too_long: field=rationale max_length=4000 actual_length=4001",
    }


def test_long_duration_does_not_create_a_source_count_quota() -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        config={"duration_target": 3600},
    )
    audit = ResearchAudit(
        passed=False,
        score=0.4,
        verdict="Final memo needs repair.",
        issues=[
            ResearchAuditIssue(
                issue_type="duplicate_or_stale_atom",
                severity="major",
                target_step="research_finalizer",
                description="Merge duplicate atom drafts.",
            )
        ],
    )

    report = build_corpus_gap_report(
        run,  # type: ignore[arg-type]
        trigger_phase="research_audit",
        round_number=1,
        source_map=SourceMap(evidence_ids=[str(uuid.uuid4())]),
        research_audit=audit,
    )

    assert report.should_acquire is False
    assert report.gaps == []


def test_audit_acquisition_gap_preserves_related_source_lineage() -> None:
    source_id = str(uuid.uuid4())
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        config={"duration_target": 1800},
    )
    inventory = SourceMap(
        source_cards=[
            SourceCard(
                source_id=source_id,
                title="Existing source",
                source_type="web",
                summary="Existing source summary.",
            )
        ]
    )
    audit = ResearchAudit(
        passed=False,
        score=0.2,
        verdict="Acquire a missing perspective.",
        issues=[
            ResearchAuditIssue(
                severity="blocking",
                target_step="source_acquisition",
                description="Find an independent account of the disputed mechanism.",
                source_refs=["S01"],
            )
        ],
    )

    report = build_corpus_gap_report(
        run,  # type: ignore[arg-type]
        trigger_phase="research_audit",
        round_number=1,
        source_map=inventory,
        research_audit=audit,
    )

    assert report.gaps[0].related_source_refs == ["S01"]
    model_payload = _model_gap_report(report, build_source_ref_context(inventory))
    assert model_payload["gaps"][0]["related_source_refs"] == ["S01"]


class _SourceDb:
    def __init__(self, source: Source | None = None) -> None:
        self.source = source
        self.commits = 0
        self.rollbacks = 0

    def add(self, source: Source) -> None:
        if source.id is None:
            source.id = uuid.uuid4()
        self.source = source

    async def get(self, _model, _source_id):
        return self.source

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(self, _query):
        source = self.source

        class Result:
            def scalars(self):
                return [source] if source is not None else []

        return Result()


@pytest.mark.asyncio
async def test_acquisition_promotes_only_after_ingest_and_provider_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    db = _SourceDb(source)
    events: list[str] = []

    async def ensure_versions(_db, current_source):
        assert current_source.is_temporary is True
        events.append("materialize")
        return [SimpleNamespace(status="ready", is_active=True)]

    async def ingest(current_source, _db, **_kwargs):
        assert current_source.is_temporary is True
        events.append("ingest")

    async def sync(_db, current_source):
        assert current_source.is_temporary is True
        assert current_source.ingestion_status == IngestionStatus.COMPLETED
        events.append("sync")
        return _ready_sync_summary()

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", ingest)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", sync)

    await native_first_ingest_candidate(db, source.id)  # type: ignore[arg-type]

    assert events == ["materialize", "ingest", "sync"]
    assert source.is_temporary is False
    assert source.ingestion_status == IngestionStatus.COMPLETED
    assert source.metadata_["promoted_after_ingest"] is True


@pytest.mark.asyncio
async def test_acquisition_failure_never_syncs_or_promotes(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    db = _SourceDb(source)

    async def ensure_versions(_db, _source):
        return []

    async def fail_ingest(_source, _db, **_kwargs):
        raise RuntimeError("broken source")

    async def unexpected_sync(*_args, **_kwargs):  # pragma: no cover - defensive
        raise AssertionError("failed source must not enter the project provider corpus")

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", fail_ingest)
    monkeypatch.setattr(
        "app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", unexpected_sync
    )

    with pytest.raises(RuntimeError, match="broken source"):
        await native_first_ingest_candidate(db, source.id)  # type: ignore[arg-type]

    assert source.is_temporary is True
    assert source.ingestion_status == IngestionStatus.FAILED


@pytest.mark.asyncio
async def test_ready_native_snapshot_can_promote_after_parser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    db = _SourceDb(source)
    synced = False

    async def ensure_versions(_db, _source):
        return [SimpleNamespace(status="ready", is_active=True, kind="fetched_html")]

    async def fail_parser(_source, _db, **_kwargs):
        raise RuntimeError("parser unavailable")

    async def sync(_db, current_source):
        nonlocal synced
        assert current_source.ingestion_status == IngestionStatus.COMPLETED
        synced = True
        return _ready_sync_summary()

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", fail_parser)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", sync)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest._provider_uploads_required", lambda: True)

    await native_first_ingest_candidate(db, source.id)  # type: ignore[arg-type]

    assert synced is True
    assert source.is_temporary is False
    assert source.fully_ingested is False
    assert source.metadata_["ingest_status"] == "completed_v2_native_parser_failed"


@pytest.mark.asyncio
async def test_native_snapshot_cannot_replace_missing_index_when_provider_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    db = _SourceDb(source)

    async def ensure_versions(_db, _source):
        return [SimpleNamespace(status="ready", is_active=True, kind="fetched_html")]

    async def fail_index(_source, _db, *, require_indexed_chunks: bool = False):
        assert require_indexed_chunks is True
        raise IngestionIndexingError("no searchable chunks")

    async def unexpected_sync(*_args, **_kwargs):  # pragma: no cover - defensive
        raise AssertionError("an unindexed source must not be promoted")

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", fail_index)
    monkeypatch.setattr(
        "app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", unexpected_sync
    )
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest._provider_uploads_required", lambda: False)

    with pytest.raises(IngestionIndexingError, match="no searchable chunks"):
        await native_first_ingest_candidate(db, source.id)  # type: ignore[arg-type]

    assert source.is_temporary is True
    assert source.ingestion_status == IngestionStatus.FAILED
    assert source.fully_ingested is False


@pytest.mark.asyncio
async def test_provider_sync_failure_does_not_promote_source(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    db = _SourceDb(source)

    async def ensure_versions(_db, _source):
        return [SimpleNamespace(status="ready", is_active=True, kind="fetched_html")]

    async def ingest(_source, _db, **_kwargs):
        return None

    async def failed_sync(_db, _source):
        return {
            "canonical_source_version_id": "version-1",
            "provider_vector_store_id": "vs-1",
            "ready_provider_file_count": 0,
        }

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", ingest)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", failed_sync)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest._provider_uploads_required", lambda: True)

    with pytest.raises(RuntimeError, match="provider corpus"):
        await native_first_ingest_candidate(db, source.id)  # type: ignore[arg-type]

    assert source.is_temporary is True


@pytest.mark.asyncio
async def test_reclaimed_old_worker_cannot_promote_after_new_attempt_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_source = _source()
    replacement = _source()
    old_source.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    replacement.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    replacement.project_id = old_source.project_id
    replacement.original_url = old_source.original_url
    replacement.ingestion_status = IngestionStatus.PROCESSING
    replacement.metadata_ = {"auto_acquired_by_v2": True}

    class Db(_SourceDb):
        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return [old_source, replacement]

            return Result()

    db = Db(old_source)

    async def ensure_versions(_db, _source):
        return [SimpleNamespace(status="ready", is_active=True, kind="fetched_html")]

    async def ingest(_source, _db, **_kwargs):
        return None

    async def sync(_db, _source):
        return _ready_sync_summary()

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", ingest)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", sync)

    with pytest.raises(RuntimeError, match="superseded by a newer attempt"):
        await native_first_ingest_candidate(db, old_source.id)  # type: ignore[arg-type]

    assert old_source.is_temporary is True


@pytest.mark.asyncio
async def test_reclaimed_old_worker_cannot_promote_without_a_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    source.metadata_ = {
        "auto_acquired_by_v2": True,
        "acquisition_claim_token": "original-claim",
    }
    db = _SourceDb(source)
    synced = False

    async def ensure_versions(_db, _source):
        return [SimpleNamespace(status="ready", is_active=True, kind="fetched_html")]

    async def ingest(current_source, _db, **_kwargs):
        current_source.metadata_ = {
            **(current_source.metadata_ or {}),
            "acquisition_claim_token": "reclaimer-claim",
            "vetting_status": "stale_ingest_reclaimed",
        }

    async def sync(*_args, **_kwargs):
        nonlocal synced
        synced = True
        return _ready_sync_summary()

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", ingest)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", sync)

    with pytest.raises(RuntimeError, match="reclaimed before promotion"):
        await native_first_ingest_candidate(db, source.id)  # type: ignore[arg-type]

    assert synced is False
    assert source.is_temporary is True


@pytest.mark.asyncio
async def test_new_attempt_promotes_even_if_reclaimed_old_worker_is_still_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_source = _source(status=IngestionStatus.PROCESSING)
    replacement = _source()
    old_source.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    replacement.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    old_source.metadata_ = {"auto_acquired_by_v2": True}
    replacement.metadata_ = {"auto_acquired_by_v2": True}
    replacement.project_id = old_source.project_id
    replacement.original_url = old_source.original_url

    class Db(_SourceDb):
        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return [old_source, replacement]

            return Result()

    db = Db(replacement)

    async def ensure_versions(_db, _source):
        return [SimpleNamespace(status="ready", is_active=True, kind="fetched_html")]

    async def ingest(_source, _db, **_kwargs):
        return None

    async def sync(_db, _source):
        return _ready_sync_summary()

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", ingest)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", sync)

    await native_first_ingest_candidate(db, replacement.id)  # type: ignore[arg-type]

    assert replacement.is_temporary is False
    assert old_source.is_temporary is True


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_is_temporary", [False, True])
async def test_manual_or_permanent_duplicate_always_blocks_acquisition_promotion(
    monkeypatch: pytest.MonkeyPatch,
    existing_is_temporary: bool,
) -> None:
    existing = _source(temporary=existing_is_temporary, status=IngestionStatus.COMPLETED)
    acquired = _source()
    existing.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    acquired.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    existing.metadata_ = {} if existing_is_temporary else {"auto_acquired_by_v2": True}
    acquired.metadata_ = {"auto_acquired_by_v2": True}
    acquired.project_id = existing.project_id
    acquired.original_url = existing.original_url

    class Db(_SourceDb):
        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return [existing, acquired]

            return Result()

    db = Db(acquired)

    async def ensure_versions(_db, _source):
        return [SimpleNamespace(status="ready", is_active=True, kind="fetched_html")]

    async def ingest(_source, _db, **_kwargs):
        return None

    async def sync(_db, _source):
        return _ready_sync_summary()

    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.ensure_source_versions", ensure_versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.run_ingestion", ingest)
    monkeypatch.setattr("app.generation_v2.source_acquisition_ingest.sync_source_to_project_corpus_now", sync)

    with pytest.raises(RuntimeError, match="blocked by an existing project source"):
        await native_first_ingest_candidate(db, acquired.id)  # type: ignore[arg-type]

    assert acquired.is_temporary is True


@pytest.mark.asyncio
@pytest.mark.parametrize("promoted", [True, False])
async def test_acquisition_tool_output_never_exposes_internal_ids(
    monkeypatch: pytest.MonkeyPatch,
    promoted: bool,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    db = _SourceDb()
    toolset = SourceAcquisitionToolset(db=db, run=run, gap_report=_gap_report(), round_number=1)  # type: ignore[arg-type]
    cleanup_calls: list[uuid.UUID] = []

    async def valid(_self, _candidate):
        return None

    async def ingest(_db, _source_id):
        assert db.source is not None
        if not promoted:
            raise RuntimeError("ingest failed")
        db.source.is_temporary = False
        db.source.ingestion_status = IngestionStatus.COMPLETED
        db.source.fully_ingested = True

    async def versions(_db, _source_id):
        return [SimpleNamespace(id=uuid.uuid4())]

    async def no_save(_self, _candidate):
        return None

    async def cleanup(_db, *, source_id, cleanup_status):
        assert cleanup_status == "acquisition_failed"
        cleanup_calls.append(source_id)

    monkeypatch.setattr(SourceAcquisitionToolset, "_validation_rejection", valid)
    monkeypatch.setattr(SourceAcquisitionToolset, "_save_candidate", no_save)
    monkeypatch.setattr("app.generation_v2.source_acquisition_toolset._native_first_ingest_candidate", ingest)
    monkeypatch.setattr("app.generation_v2.source_acquisition_toolset._source_versions", versions)
    monkeypatch.setattr("app.generation_v2.source_acquisition_toolset.mark_provider_files_inactive", cleanup)

    output = await toolset.ingest_source_candidate_tool(
        {
            "url": "https://example.com/report",
            "gap_ids": ["gap-1"],
            "expected_editorial_function": "mechanism",
            "rationale": "Primary report",
            "confidence": 0.9,
            "source_type_hint": "web",
            "authority_hint": "official",
        }
    )

    assert forbidden_tool_output_keys(output) == set()
    assert "source_id" not in output
    assert "source_version_ids" not in output
    assert model_facing_tool_output("ingest_source_candidate", output) == output
    assert db.source is not None
    assert db.source.is_temporary is (not promoted)
    if not promoted:
        assert db.source.ingestion_status == IngestionStatus.FAILED
        assert output["reason"] != "ingest failed"
        assert toolset.candidates[0].rejection_reason == "ingest failed"
        assert cleanup_calls == [db.source.id]
    else:
        assert cleanup_calls == []


@pytest.mark.asyncio
async def test_failed_or_staged_url_does_not_block_retry() -> None:
    failed_staged = _source(temporary=True, status=IngestionStatus.FAILED)
    failed_legacy = _source(temporary=False, status=IngestionStatus.FAILED)

    class Result:
        def scalars(self):
            return [failed_staged, failed_legacy]

    class Db:
        async def execute(self, _query):
            return Result()

    existing = await existing_source_for_url(Db(), failed_staged.project_id, failed_staged.original_url)  # type: ignore[arg-type]

    assert existing is None


@pytest.mark.asyncio
async def test_active_staged_url_blocks_parallel_duplicate() -> None:
    staged = _source(temporary=True, status=IngestionStatus.PROCESSING)

    class Result:
        def scalars(self):
            return [staged]

    class Db:
        async def execute(self, _query):
            return Result()

    existing = await existing_source_for_url(Db(), staged.project_id, staged.original_url)  # type: ignore[arg-type]

    assert existing is staged


@pytest.mark.asyncio
async def test_expired_staged_url_is_reclaimed_and_retryable() -> None:
    staged = _source(temporary=True, status=IngestionStatus.PROCESSING)
    staged.metadata_ = {"acquisition_lease_expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}

    class Result:
        def scalars(self):
            return [staged]

    class Db:
        async def execute(self, _query):
            return Result()

    reclaimed: list[uuid.UUID] = []
    existing = await existing_source_for_url(  # type: ignore[arg-type]
        Db(),
        staged.project_id,
        staged.original_url,
        reclaimed_source_ids=reclaimed,
    )

    assert existing is None
    assert reclaimed == [staged.id]
    assert staged.ingestion_status == IngestionStatus.FAILED
    assert staged.metadata_["vetting_status"] == "stale_ingest_reclaimed"


@pytest.mark.asyncio
async def test_reclaimed_staged_source_is_cleaned_even_when_candidate_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    db = _SourceDb()
    toolset = SourceAcquisitionToolset(db=db, run=run, gap_report=_gap_report(), round_number=1)  # type: ignore[arg-type]
    reclaimed_id = uuid.uuid4()
    events: list[str] = []

    async def reject_after_reclaim(_self, _candidate):
        toolset.reclaimed_stale_source_ids.append(reclaimed_id)
        return "rejected", "Same-domain acquisition cap reached for this round/run."

    async def stage_cleanup(_db, *, source_id, cleanup_reason):
        assert source_id == reclaimed_id
        assert cleanup_reason == "stale_acquisition_reclaimed"
        events.append("staged")
        return []

    async def finish_cleanup(_db, *, source_id, cleanup_status):
        assert source_id == reclaimed_id
        assert cleanup_status == "stale_acquisition_reclaimed"
        assert db.commits == 1
        events.append("detached")

    async def no_save(_self, _candidate):
        return None

    monkeypatch.setattr(SourceAcquisitionToolset, "_validation_rejection", reject_after_reclaim)
    monkeypatch.setattr(SourceAcquisitionToolset, "_save_candidate", no_save)
    monkeypatch.setattr(
        "app.generation_v2.source_acquisition_toolset.stage_provider_files_inactive",
        stage_cleanup,
    )
    monkeypatch.setattr(
        "app.generation_v2.source_acquisition_toolset.mark_provider_files_inactive",
        finish_cleanup,
    )

    output = await toolset.ingest_source_candidate_tool(
        {
            "url": "https://example.com/rejected-after-reclaim",
            "gap_ids": ["gap-1"],
            "rationale": "Candidate",
            "confidence": 0.9,
        }
    )

    assert output["accepted"] is False
    assert events == ["staged", "detached"]
    assert toolset.reclaimed_stale_source_ids == []


def test_source_repair_round_limit_is_hard_capped_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.settings.generation_v2_source_acquisition_max_rounds", 3)

    assert remaining_acquisition_rounds(0) == 1
    assert remaining_acquisition_rounds(1) == 0


@pytest.mark.asyncio
async def test_only_terminal_acquisition_round_consumes_budget_after_worker_restart() -> None:
    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Db:
        def __init__(self, value):
            self.value = value
            self.query_params = []

        async def execute(self, query):
            self.query_params.append(query.compile().params)
            return Result(self.value)

    run_id = uuid.uuid4()
    db = Db(uuid.uuid4())
    assert await persisted_source_acquisition_rounds_used(db, run_id) == 1  # type: ignore[arg-type]
    assert any(V2ArtifactType.SOURCE_ACQUISITION_ROUND in params.values() for params in db.query_params)
    assert await persisted_source_acquisition_rounds_used(Db(None), run_id) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_interrupted_staged_candidate_is_restored_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    source = _source(temporary=True, status=IngestionStatus.PROCESSING)
    source.project_id = run.project_id
    source.generation_run_id = run.id
    source.metadata_ = {
        "auto_acquired_by_v2": True,
        "acquisition_round": 1,
        "candidate_url": source.original_url,
        "gap_ids": ["gap-1"],
    }
    persisted = SourceAcquisitionCandidate(
        url=source.original_url,
        normalized_url=source.original_url,
        gap_ids=["gap-1"],
        rationale="Interrupted candidate",
        confidence=0.9,
        status="accepted",
        ingest_status="pending",
    )

    class Result:
        def scalars(self):
            return [source]

    class Db:
        async def execute(self, _query):
            return Result()

    retried: list[uuid.UUID] = []

    async def load_candidates(_db, _run_id, _round_number):
        return [persisted]

    async def retry(_self, current_source, candidate):
        retried.append(current_source.id)
        current_source.is_temporary = False
        current_source.ingestion_status = IngestionStatus.COMPLETED
        candidate.status = "ingested"
        candidate.ingest_status = "completed"

    async def no_save(_self, _candidate):
        return None

    monkeypatch.setattr(
        "app.generation_v2.source_acquisition_toolset._persisted_round_candidates",
        load_candidates,
    )
    monkeypatch.setattr(SourceAcquisitionToolset, "_ingest_staged_candidate", retry)
    monkeypatch.setattr(SourceAcquisitionToolset, "_save_candidate", no_save)

    toolset = SourceAcquisitionToolset(
        db=Db(),  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        gap_report=_gap_report(),
        round_number=1,
    )
    await toolset.restore_unfinished_round()

    assert retried == [source.id]
    assert toolset.candidates == [persisted]
    assert persisted.status == "ingested"
    assert toolset.ingested_count == 1


@pytest.mark.asyncio
async def test_triage_repair_budget_distinguishes_pending_resume_from_completed_work() -> None:
    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Db:
        def __init__(self, *values):
            self.values = list(values)

        async def execute(self, _query):
            return Result(self.values.pop(0))

    run_id = uuid.uuid4()
    reserved = SimpleNamespace(payload={"repair_operation": {"status": "reserved", "payload": {}}})
    in_progress = SimpleNamespace(payload={"repair_operation": {"status": "in_progress", "payload": {}}})
    completed = SimpleNamespace(payload={"repair_operation": {"status": "completed", "payload": {}}})

    assert await source_triage_repair_already_used(Db(reserved), run_id) is False  # type: ignore[arg-type]
    assert await source_triage_repair_already_used(Db(in_progress), run_id) is False  # type: ignore[arg-type]
    assert await source_triage_repair_already_used(Db(completed), run_id) is True  # type: ignore[arg-type]
    assert await source_triage_repair_already_used(Db(None, uuid.uuid4()), run_id) is True  # type: ignore[arg-type]
    assert await source_triage_repair_already_used(Db(None, None), run_id) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_source_repair_stops_after_three_candidates() -> None:
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    toolset = SourceAcquisitionToolset(
        db=_SourceDb(),  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        gap_report=_gap_report(),
        round_number=1,
        candidates=[SourceAcquisitionCandidate(url=f"https://example.com/{index}") for index in range(3)],
    )

    output = await toolset.ingest_source_candidate_tool({"url": "https://example.com/fourth"})

    assert output["accepted"] is False
    assert output["error"] == "candidate_budget_exhausted"
    assert len(toolset.candidates) == 3


def test_failed_candidate_url_remains_retryable_at_candidate_cap() -> None:
    failed_url = "https://example.com/retry"
    failed = SourceAcquisitionCandidate(url=failed_url, normalized_url=failed_url, status="failed")
    toolset = SourceAcquisitionToolset(
        db=_SourceDb(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4()),  # type: ignore[arg-type]
        gap_report=_gap_report(),
        round_number=1,
        candidates=[
            failed,
            SourceAcquisitionCandidate(url="https://example.com/two"),
            SourceAcquisitionCandidate(url="https://example.com/three"),
        ],
    )

    assert toolset._candidate_budget_error(candidate_url=failed_url) is None
    retried = failed.model_copy(update={"status": "ingested", "ingest_status": "completed"})
    toolset._remember_candidate(retried)
    assert len(toolset.candidates) == 3
    assert toolset._candidate_for_url(failed_url) is retried


@pytest.mark.asyncio
async def test_failed_provider_detach_is_durable_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.settings.openai_api_key", "test-key")
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        provider="openai",
        provider_vector_store_id="vs-test",
        scope="project",
        status="ready",
        config={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_version_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        provider="openai",
        provider_file_id="file-test",
        attributes={
            "is_active": True,
            "agent_facing": True,
            "canonical_retrieval_surface": True,
        },
        status="ready",
    )

    class Result:
        def scalars(self):
            return [provider_file]

    class Db:
        async def get(self, model, _item_id):
            return provider_index if model is ProviderCorpusIndex else None

        async def execute(self, _query):
            return Result()

        async def flush(self):
            return None

        async def commit(self):
            return None

    synced_attributes: list[dict] = []

    async def sync_attributes(_index, _file, attributes, *, force=False):
        assert force is True
        synced_attributes.append(attributes)
        return True

    async def failed_detach(_index, _file_id):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "app.generation_v2.source_corpus_provider_detach.sync_provider_file_attributes",
        sync_attributes,
    )
    monkeypatch.setattr(
        "app.generation_v2.source_corpus_provider_detach.detach_openai_vector_store_file",
        failed_detach,
    )

    detached = await detach_provider_file_from_active_index(Db(), provider_file)  # type: ignore[arg-type]

    assert detached is False
    assert synced_attributes[0]["is_active"] is False
    assert synced_attributes[0]["agent_facing"] is False
    assert provider_file.cleanup_status == "provider_detach_retry_pending"
    assert provider_file.attributes["active_index_status"] == "inactive_detach_retry_pending"

    async def successful_detach(_index, _file_id):
        return None

    monkeypatch.setattr(
        "app.generation_v2.source_corpus_provider_detach.detach_openai_vector_store_file",
        successful_detach,
    )

    assert await retry_pending_provider_detaches(Db(), provider_index) == 1  # type: ignore[arg-type]
    assert provider_file.cleanup_status == "removed_from_active_index_retained"
    assert provider_file.attributes["active_index_status"] == "removed"


@pytest.mark.asyncio
async def test_provider_detach_without_api_key_stays_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.settings.openai_api_key", "")
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        provider="openai",
        provider_file_id="file-still-external",
        attributes={"is_active": True},
        status="ready",
    )

    class Db:
        async def flush(self):
            return None

        async def commit(self):
            return None

    detached = await detach_provider_file_from_active_index(Db(), provider_file)  # type: ignore[arg-type]

    assert detached is False
    assert provider_file.cleanup_status == "provider_detach_retry_pending"
    assert provider_file.attributes["active_index_status"] == "detach_retry_pending"


@pytest.mark.asyncio
async def test_private_candidate_is_rejected_before_db_or_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = SourceAcquisitionCandidate(
        url="http://127.0.0.1/admin",
        normalized_url="http://127.0.0.1/admin",
        gap_ids=["gap-1"],
        expected_editorial_function="mechanism",
        rationale="Looks relevant",
        confidence=0.9,
    )
    owner = SimpleNamespace(ingested_count=0, gap_report=_gap_report())

    def blocked(_url):
        raise ValueError("SSRF blocked")

    monkeypatch.setattr("app.generation_v2.source_acquisition_limits._check_ssrf", blocked)

    assert await validation_rejection(owner, candidate) == ("rejected", "SSRF blocked")


@pytest.mark.asyncio
async def test_rendered_browser_blocks_unsafe_subrequests(monkeypatch: pytest.MonkeyPatch) -> None:
    class Route:
        aborted = False

        async def abort(self, _reason):
            self.aborted = True

        async def continue_(self):
            raise AssertionError("unsafe request must not continue")

    route = Route()

    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, *_args, **_kwargs):
            await self.handler(route, SimpleNamespace(url="http://169.254.169.254/latest/meta-data"))
            return None

    def validate(url: str) -> None:
        if "169.254.169.254" in url:
            raise ValueError("SSRF blocked")

    monkeypatch.setattr("app.generation_v2.safe_browser.validate_public_browser_url", validate)

    with pytest.raises(ValueError, match="unsafe request"):
        await guarded_page_goto(Page(), "https://public.example/article", wait_until="networkidle", timeout=1000)

    assert route.aborted is True


@pytest.mark.asyncio
async def test_rendered_browser_ignores_public_cross_origin_assets_without_failing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Route:
        aborted = False

        async def abort(self, _reason):
            self.aborted = True

        async def continue_(self):
            raise AssertionError("cross-origin asset must not continue")

    route = Route()

    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, *_args, **_kwargs):
            await self.handler(route, SimpleNamespace(url="https://cdn.example/article.css"))
            return "document-response"

    monkeypatch.setattr("app.generation_v2.safe_browser.validate_public_browser_url", lambda _url: None)

    response = await guarded_page_goto(
        Page(),
        "https://public.example/article",
        wait_until="networkidle",
        timeout=1000,
    )

    assert response == "document-response"
    assert route.aborted is True


@pytest.mark.asyncio
async def test_rendered_browser_validates_final_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, *_args, **_kwargs):
            self.url = "http://127.0.0.1/admin"
            return None

    def validate(url: str) -> None:
        if "127.0.0.1" in url:
            raise ValueError("SSRF blocked redirect")

    monkeypatch.setattr("app.generation_v2.safe_browser.validate_public_browser_url", validate)

    with pytest.raises(ValueError, match="blocked redirect"):
        await guarded_page_goto(Page(), "https://public.example/article", wait_until="networkidle", timeout=1000)


@pytest.mark.asyncio
async def test_rendered_browser_blocks_unsafe_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    class WebSocket:
        url = "ws://127.0.0.1/admin"
        closed = False

        async def close(self, **_kwargs):
            self.closed = True

        def connect_to_server(self):  # pragma: no cover - defensive
            raise AssertionError("unsafe websocket must not connect")

    websocket = WebSocket()

    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, _handler):
            return None

        async def route_web_socket(self, _pattern, handler):
            self.websocket_handler = handler

        async def goto(self, *_args, **_kwargs):
            await self.websocket_handler(websocket)
            return None

    def validate(url: str) -> None:
        if "127.0.0.1" in url:
            raise ValueError("SSRF blocked websocket")

    monkeypatch.setattr("app.generation_v2.safe_browser.validate_public_browser_url", validate)

    with pytest.raises(ValueError, match="unsafe request"):
        await guarded_page_goto(Page(), "https://public.example/article", wait_until="networkidle", timeout=1000)

    assert websocket.closed is True


@pytest.mark.asyncio
async def test_http_transport_connects_to_validated_ip_not_rebound_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import web as web_module

    connected_hosts: list[str] = []

    class Backend:
        async def connect_tcp(self, host, _port, **_kwargs):
            connected_hosts.append(host)
            return SimpleNamespace()

        async def sleep(self, _seconds):
            return None

    monkeypatch.setattr(web_module, "_validated_public_ip_strings", lambda _host: ["93.184.216.34"])
    backend = web_module._SSRFGuardedNetworkBackend(Backend())  # type: ignore[arg-type]

    await backend.connect_tcp("rebind.example", 443)

    assert connected_hosts == ["93.184.216.34"]


def test_rendered_browser_launch_pins_dns_and_blocks_other_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.generation_v2.safe_browser._validated_public_ip_strings",
        lambda _host: ["93.184.216.34"],
    )

    args = public_browser_launch_args("https://public.example/article")

    assert any("MAP public.example 93.184.216.34" in arg for arg in args)
    assert any("MAP * ~NOTFOUND" in arg for arg in args)
    assert "--disable-quic" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args


@pytest.mark.asyncio
async def test_rendered_browser_disables_peer_connections_before_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts: list[str] = []

    class Context:
        async def add_init_script(self, *, script):
            scripts.append(script)

        async def route(self, _pattern, _handler):
            return None

    class Page:
        url = "https://public.example/article"
        context = Context()

        async def goto(self, *_args, **_kwargs):
            assert scripts
            return None

    monkeypatch.setattr(
        "app.generation_v2.safe_browser.validate_public_browser_url",
        lambda _url: None,
    )

    await guarded_page_goto(
        Page(),
        "https://public.example/article",
        wait_until="networkidle",
        timeout=1000,
    )

    assert "RTCPeerConnection" in scripts[0]
    assert "RTCIceTransport" in scripts[0]


@pytest.mark.asyncio
async def test_runtime_hides_raw_tool_exception_from_model_but_keeps_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.runtime_contracts import AgentTool
    from app.generation_v2 import runtime_tool_calls as runtime_module

    secret = str(uuid.uuid4())
    traces: list[dict] = []

    async def fail(_args):
        raise RuntimeError(f"database row {secret}")

    async def save_trace(_db, **kwargs):
        traces.append(kwargs["output_json"])
        return SimpleNamespace()

    async def no_commit(_db):
        return None

    monkeypatch.setattr(runtime_module, "save_tool_trace", save_trace)
    monkeypatch.setattr(runtime_module, "_commit_runtime_side_effects", no_commit)
    monkeypatch.setattr(runtime_module, "dump_v2_debug", lambda **_kwargs: None)

    result = await runtime_module.process_runtime_function_calls(
        SimpleNamespace(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_acquisition",
        agent_name="source_acquisition",
        round_number=1,
        chain_id="chain",
        checkpoint_index=0,
        chain_round=1,
        function_calls=[{"name": "ingest_source_candidate", "call_id": "call-1", "arguments": {}}],
        local_tools=[AgentTool(name="ingest_source_candidate", description="", parameters={}, handler=fail)],
        chain_model_tool_output_chars=0,
    )

    model_output = json.loads(result.tool_outputs[0]["output"])
    assert model_output["error"] == "tool_execution_failed"
    assert secret not in result.tool_outputs[0]["output"]
    assert secret in json.dumps(traces)
