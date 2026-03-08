import uuid

import pytest

from app.models.evidence import EvidenceSpan
from app.models.source import AuthorityType, DomainReliability, IngestionStatus, Source, SourceType
from app.pipeline.phase3_research.librarian_fast import _build_search_queries, _build_targeted_query
from app.pipeline.phase3_research.tools import create_web_evidence


def test_build_targeted_query_appends_asset_keywords() -> None:
    question = "How does the HKD issuance mechanism work?"
    missing_assets = ["CASE", "STATISTIC"]

    targeted = _build_targeted_query(question, missing_assets)

    assert targeted.startswith(question)
    assert "case" in targeted.lower()
    assert "statistics" in targeted.lower()


def test_build_search_queries_adds_targeted_variant_only_when_distinct() -> None:
    question = "What happened in 1983?"
    queries = _build_search_queries(question, ["TIMELINE"])

    assert queries[0] == question
    assert len(queries) == 2
    assert queries[1] != question


class _FakeAsyncDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_calls = 0

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_calls += 1


@pytest.mark.asyncio
async def test_create_web_evidence_creates_temp_source_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeAsyncDB()
    project_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    manifest_id = str(uuid.uuid4())
    provenance_calls: list[tuple[str, str, str, str, str | None]] = []

    async def _fake_find_canonical_source_by_url(*args, **kwargs):
        return None

    async def _fake_get_or_create(**kwargs):
        evidence = EvidenceSpan(
            id=uuid.uuid4(),
            project_id=uuid.UUID(kwargs["project_id"]),
            source_id=kwargs["source_id"],
            verbatim_quote=kwargs["verbatim_quote"],
            asset_type=kwargs["asset_type"],
            narrative_value_score=kwargs["narrative_value_score"],
        )
        db.add(evidence)
        await db.flush()
        return evidence, True

    async def _fake_ensure_evidence_provenance(*, context, **kwargs):
        provenance_calls.append(
            (
                context.question_id,
                context.branch_id,
                context.asset_type,
                context.retrieved_by,
                context.direct_or_contextual,
            )
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools._find_canonical_source_by_url",
        _fake_find_canonical_source_by_url,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.get_or_create_canonical_evidence",
        _fake_get_or_create,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.ensure_evidence_provenance",
        _fake_ensure_evidence_provenance,
    )

    evidence_id = await create_web_evidence(
        url="https://reuters.com/article",
        quote="A concrete quote.",
        asset_type="case",
        project_id=project_id,
        db=db,
        generation_run_id=run_id,
        manifest_id=manifest_id,
        question_id="b6.1",
        branch_id="b6",
        query_text="Why does this matter today?",
        direct_or_contextual="direct",
        title="Reuters coverage",
    )

    sources = [item for item in db.added if isinstance(item, Source)]
    evidence_rows = [item for item in db.added if isinstance(item, EvidenceSpan)]

    assert db.flush_calls >= 2
    assert len(sources) == 1
    assert len(evidence_rows) == 1
    source = sources[0]
    evidence = evidence_rows[0]

    assert source.ingestion_status == IngestionStatus.PENDING
    assert source.is_temporary is True
    assert source.fully_ingested is False
    assert str(source.generation_run_id) == run_id
    assert source.domain_reliability == DomainReliability.MEDIUM
    assert source.authority_type == AuthorityType.SECONDARY
    assert evidence.source_id == source.id
    assert evidence_id == str(evidence.id)
    assert provenance_calls == [("b6.1", "b6", "case", "librarian_fast", "direct")]


@pytest.mark.asyncio
async def test_create_web_evidence_reuses_canonical_source(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeAsyncDB()
    project_id = str(uuid.uuid4())
    canonical_source = Source(
        id=uuid.uuid4(),
        project_id=uuid.UUID(project_id),
        source_type=SourceType.WEB,
        original_url="https://reuters.com/article",
        title="Canonical Reuters coverage",
        ingestion_status=IngestionStatus.COMPLETED,
        is_temporary=False,
        fully_ingested=True,
        domain_reliability=DomainReliability.MEDIUM,
        authority_type=AuthorityType.SECONDARY,
    )

    async def _fake_find_canonical_source_by_url(*args, **kwargs):
        return canonical_source

    async def _fake_get_or_create(**kwargs):
        evidence = EvidenceSpan(
            id=uuid.uuid4(),
            project_id=uuid.UUID(kwargs["project_id"]),
            source_id=kwargs["source_id"],
            verbatim_quote=kwargs["verbatim_quote"],
            asset_type=kwargs["asset_type"],
            narrative_value_score=kwargs["narrative_value_score"],
        )
        db.add(evidence)
        await db.flush()
        return evidence, True

    async def _fake_ensure_evidence_provenance(**kwargs):
        return None

    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools._find_canonical_source_by_url",
        _fake_find_canonical_source_by_url,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.get_or_create_canonical_evidence",
        _fake_get_or_create,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.ensure_evidence_provenance",
        _fake_ensure_evidence_provenance,
    )

    evidence_id = await create_web_evidence(
        url="https://reuters.com/article",
        quote="A concrete quote.",
        asset_type="case",
        project_id=project_id,
        db=db,
        generation_run_id=str(uuid.uuid4()),
        manifest_id=str(uuid.uuid4()),
        question_id="b6.1",
        branch_id="b6",
    )

    sources = [item for item in db.added if isinstance(item, Source)]
    evidence_rows = [item for item in db.added if isinstance(item, EvidenceSpan)]

    assert sources == []
    assert len(evidence_rows) == 1
    assert evidence_rows[0].source_id == canonical_source.id
    assert evidence_id == str(evidence_rows[0].id)
