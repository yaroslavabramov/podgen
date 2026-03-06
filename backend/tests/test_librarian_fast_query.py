import uuid

import pytest

from app.models.evidence import EvidenceSpan
from app.models.source import IngestionStatus, Source
from app.pipeline.phase3_research.librarian_fast import _build_targeted_query
from app.pipeline.phase3_research.tools import create_web_evidence


def test_build_targeted_query_does_not_append_asset_tags() -> None:
    question = "How does the HKD issuance mechanism work?"
    missing_assets = ["CASE", "ANECDOTE", "STATISTIC"]
    assert _build_targeted_query(question, missing_assets) == question


class _FakeAsyncDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_calls = 0

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_calls += 1


@pytest.mark.asyncio
async def test_create_web_evidence_creates_pending_temp_source_for_deep_ingestion() -> None:
    db = _FakeAsyncDB()
    project_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    evidence_id = await create_web_evidence(
        url="https://example.com/article",
        quote="A concrete quote.",
        asset_type="case",
        project_id=project_id,
        db=db,
        generation_run_id=run_id,
    )

    sources = [item for item in db.added if isinstance(item, Source)]
    evidence_rows = [item for item in db.added if isinstance(item, EvidenceSpan)]

    assert db.flush_calls == 2
    assert len(sources) == 1
    assert len(evidence_rows) == 1
    source = sources[0]
    evidence = evidence_rows[0]

    assert source.ingestion_status == IngestionStatus.PENDING
    assert source.is_temporary is True
    assert source.fully_ingested is False
    assert str(source.generation_run_id) == run_id
    assert evidence.source_id == source.id
    assert evidence_id == str(evidence.id)
