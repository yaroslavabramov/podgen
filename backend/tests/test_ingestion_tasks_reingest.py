import uuid
from types import SimpleNamespace

import pytest

from app.models.source import SourceType
from app.workers.ingestion_tasks import _reset_source_for_reingestion


class _FakeAsyncDB:
    def __init__(self) -> None:
        self.executed = []
        self.flushed = False

    async def execute(self, statement):
        self.executed.append(statement)
        return None

    async def flush(self) -> None:
        self.flushed = True


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
