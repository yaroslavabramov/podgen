import uuid
from types import SimpleNamespace

import pytest

from app.models.claim import VerificationLevel
from app.models.source import SourceType
from app.workers.ingestion_tasks import _deep_ingest_async, _reset_source_for_reingestion, _upgrade_claims_for_source


class _FakeAsyncDB:
    def __init__(self) -> None:
        self.executed = []
        self.flushed = False

    async def execute(self, statement):
        self.executed.append(statement)
        return None

    async def flush(self) -> None:
        self.flushed = True


class _FakeResult:
    def __init__(self, *, rows=None, scalar=None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


class _FakeUpgradeDB:
    def __init__(self, *, source_id, claim, claim_id, evidence_id) -> None:
        self.source_id = source_id
        self.claim = claim
        self.claim_id = claim_id
        self.evidence_id = evidence_id
        self.commits = 0

    async def execute(self, statement):
        sql = str(statement).lower()
        if "from evidence_spans" in sql:
            return _FakeResult(rows=[(self.evidence_id,)])
        if "from claim_evidence" in sql:
            return _FakeResult(rows=[(self.claim_id,)])
        if "from claims" in sql:
            return _FakeResult(scalar=self.claim)
        raise AssertionError(f"Unexpected SQL in test fake DB: {statement}")

    async def commit(self) -> None:
        self.commits += 1


class _FakeDeepIngestResult:
    def __init__(self, scalar) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDeepIngestDB:
    def __init__(self, source) -> None:
        self.source = source
        self.commit_calls = 0
        self.rollback_calls = 0
        self.execute_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        self.execute_calls += 1
        return _FakeDeepIngestResult(self.source)

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_reset_source_for_reingestion_detaches_evidence_before_chunk_delete(monkeypatch) -> None:
    source_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        source_type=SourceType.WEB,
        fully_ingested=True,
        raw_text_path="raw_text/path.md",
        text_preview="preview",
        published_at="2026-02-25",
        title="Some title",
        metadata_={"error": "boom", "web_quality": {"state": "ok"}, "keep": "value"},
    )
    db = _FakeAsyncDB()

    deleted_sources: list[str] = []

    def _fake_delete_points(source_id_arg: str) -> None:
        deleted_sources.append(source_id_arg)

    monkeypatch.setattr(
        "app.services.embeddings.delete_points_by_source_id",
        _fake_delete_points,
    )

    await _reset_source_for_reingestion(source, db)

    assert deleted_sources == [str(source_id)]
    assert db.flushed is True
    assert len(db.executed) == 2

    update_stmt = db.executed[0]
    delete_stmt = db.executed[1]
    assert update_stmt.__visit_name__ == "update"
    assert update_stmt.table.name == "evidence_spans"
    assert "chunk_id IS NOT NULL" in str(update_stmt)
    assert delete_stmt.__visit_name__ == "delete"
    assert delete_stmt.table.name == "chunks"

    assert source.fully_ingested is False
    assert source.raw_text_path is None
    assert source.text_preview is None
    assert source.published_at is None
    assert source.title is None
    assert source.metadata_ == {"keep": "value"}


@pytest.mark.asyncio
async def test_upgrade_claims_for_source_reuses_generation_run_verifier_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    generation_run_id = str(uuid.uuid4())
    claim = SimpleNamespace(id=claim_id, verification_level=VerificationLevel.UNVERIFIED)
    db = _FakeUpgradeDB(
        source_id=source_id,
        claim=claim,
        claim_id=claim_id,
        evidence_id=evidence_id,
    )

    captured = {}

    async def _fake_load_context(*, generation_run_id, db):
        captured["context_run_id"] = generation_run_id
        captured["context_db"] = db
        return {"q1": {"question": "How does it work?", "intent": "explain"}}

    async def _fake_verify_single_claim(claim_arg, db_arg, *, generation_run_id=None, question_context_map=None):
        captured["verify_claim_id"] = claim_arg.id
        captured["verify_db"] = db_arg
        captured["verify_run_id"] = generation_run_id
        captured["verify_question_context_map"] = question_context_map
        return VerificationLevel.OBSERVED

    monkeypatch.setattr(
        "app.workers.ingestion_tasks._load_reverify_question_context_map",
        _fake_load_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.verify_single_claim",
        _fake_verify_single_claim,
    )

    upgraded = await _upgrade_claims_for_source(
        source_id,
        db,
        generation_run_id=generation_run_id,
    )

    assert upgraded == 1
    assert claim.verification_level == VerificationLevel.OBSERVED
    assert db.commits == 1
    assert captured["context_run_id"] == generation_run_id
    assert captured["verify_claim_id"] == claim_id
    assert captured["verify_db"] is db
    assert captured["verify_run_id"] == generation_run_id
    assert captured["verify_question_context_map"] == {
        "q1": {"question": "How does it work?", "intent": "explain"}
    }


@pytest.mark.asyncio
async def test_deep_ingest_async_rolls_back_then_marks_source_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        ingestion_status="pending",
        metadata_={},
        is_temporary=True,
        fully_ingested=False,
        title="Temp source",
    )
    db = _FakeDeepIngestDB(source)

    monkeypatch.setattr(
        "app.workers.worker_db.worker_session_factory",
        lambda: db,
    )
    monkeypatch.setattr(
        "app.realtime.publisher.publish_event",
        lambda *args, **kwargs: None,
    )

    async def _fake_run_ingestion(source_arg, db_arg):
        raise RuntimeError("deadlock while canonicalizing")

    monkeypatch.setattr("app.ingestion.router.run_ingestion", _fake_run_ingestion)

    with pytest.raises(RuntimeError, match="deadlock while canonicalizing"):
        await _deep_ingest_async(str(source.id), str(uuid.uuid4()))

    assert db.rollback_calls == 1
    assert db.commit_calls == 2
    assert source.ingestion_status == "failed"
    assert source.metadata_["error"] == "deadlock while canonicalizing"
