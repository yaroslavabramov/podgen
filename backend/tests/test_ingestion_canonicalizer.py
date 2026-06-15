import uuid
from datetime import datetime

import pytest

from app.ingestion.canonicalizer import (
    canonicalize_source,
    _date_extraction_gate,
    _extract_date,
    _propagate_canonical_metadata_to_duplicates,
)
from app.models.source import AuthorityType, DomainReliability, Source, SourceType


def _make_web_source(*, text_preview: str, metadata: dict | None = None) -> Source:
    return Source(
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        original_url="https://example.com/article",
        text_preview=text_preview,
        metadata_=metadata,
    )


def test_date_gate_skips_blocked_sources() -> None:
    source = _make_web_source(
        text_preview="Some preview text that is long enough.",
        metadata={"web_quality": {"state": "blocked"}},
    )

    eligible, reason = _date_extraction_gate(source, min_chars=10)

    assert not eligible
    assert reason == "blocked_by_gate_a"


def test_date_gate_skips_short_preview() -> None:
    source = _make_web_source(text_preview="tiny")

    eligible, reason = _date_extraction_gate(source, min_chars=10)

    assert not eligible
    assert reason == "preview_too_short"


@pytest.mark.asyncio
async def test_extract_date_parses_fenced_json(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call(**kwargs) -> str:
        return '```json\n{"date":"2023-07-01"}\n```'

    monkeypatch.setattr("app.ingestion.canonicalizer.call_haiku_structured", _fake_call)
    source = _make_web_source(text_preview="Long enough preview text for extraction.")

    published_at, status, reason = await _extract_date(source)

    assert published_at == datetime(2023, 7, 1)
    assert status == "success"
    assert reason == "extracted"


@pytest.mark.asyncio
async def test_extract_date_prompt_requires_explicit_provided_date(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def _fake_call(**kwargs) -> str:
        captured["system"] = kwargs["system"]
        captured["prompt"] = kwargs["prompt"]
        return '{"date": null}'

    monkeypatch.setattr("app.ingestion.canonicalizer.call_haiku_structured", _fake_call)
    source = _make_web_source(text_preview="Long enough preview text for extraction.")

    await _extract_date(source)

    assert "Extract publication dates only from provided fields" in captured["system"]
    assert "Use only an explicit date visible in the title, URL path, or text preview above." in captured["prompt"]
    assert "Do not infer from crawl date, current date, topic recency, or outside knowledge." in captured["prompt"]


@pytest.mark.asyncio
async def test_extract_date_handles_missing_date(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call(**kwargs) -> str:
        return '{"date": null}'

    monkeypatch.setattr("app.ingestion.canonicalizer.call_haiku_structured", _fake_call)
    source = _make_web_source(text_preview="Long enough preview text for extraction.")

    published_at, status, reason = await _extract_date(source)

    assert published_at is None
    assert status == "not_found"
    assert reason == "empty_or_null_date"


@pytest.mark.asyncio
async def test_extract_date_handles_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call(**kwargs) -> str:
        return "not-json"

    monkeypatch.setattr("app.ingestion.canonicalizer.call_haiku_structured", _fake_call)
    source = _make_web_source(text_preview="Long enough preview text for extraction.")

    published_at, status, reason = await _extract_date(source)

    assert published_at is None
    assert status == "parse_error"
    assert reason == "invalid_json"


def test_propagate_canonical_metadata_to_duplicates() -> None:
    canonical = Source(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        original_url="https://example.com/article",
        domain_reliability=DomainReliability.MEDIUM,
        authority_type=AuthorityType.SECONDARY,
    )
    duplicate = Source(
        id=uuid.uuid4(),
        project_id=canonical.project_id,
        source_type=SourceType.WEB,
        original_url="https://www.example.com/article",
        is_duplicate_of=canonical.id,
        domain_reliability=DomainReliability.UNKNOWN,
        authority_type=AuthorityType.UNKNOWN,
    )

    _propagate_canonical_metadata_to_duplicates([canonical, duplicate])

    assert duplicate.domain_reliability == DomainReliability.MEDIUM
    assert duplicate.authority_type == AuthorityType.SECONDARY


class _FakeScalarResult:
    def __init__(self, scalar=None, rows=None) -> None:
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeCanonicalizeDB:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.flush_calls = 0

    async def execute(self, statement):
        return self._results.pop(0)

    async def flush(self) -> None:
        self.flush_calls += 1


@pytest.mark.asyncio
async def test_canonicalize_source_only_updates_matching_duplicate_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    canonical = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://example.com/article",
        title="Canonical",
        text_preview="",
        created_at=datetime(2024, 1, 1),
        domain_reliability=DomainReliability.UNKNOWN,
        authority_type=AuthorityType.UNKNOWN,
    )
    duplicate = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://www.example.com/article",
        title="Duplicate",
        text_preview="",
        created_at=datetime(2024, 1, 2),
        domain_reliability=DomainReliability.UNKNOWN,
        authority_type=AuthorityType.UNKNOWN,
    )
    unrelated = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://other.example.com/post",
        title="Other",
        text_preview="",
        created_at=datetime(2024, 1, 3),
        domain_reliability=DomainReliability.UNKNOWN,
        authority_type=AuthorityType.UNKNOWN,
    )
    db = _FakeCanonicalizeDB(
        [
            _FakeScalarResult(scalar=duplicate),
            _FakeScalarResult(rows=[canonical, duplicate, unrelated]),
        ]
    )

    async def _fake_classify(source):
        assert source.id == canonical.id
        return AuthorityType.SECONDARY

    monkeypatch.setattr(
        "app.ingestion.canonicalizer.classify_source_authority",
        _fake_classify,
    )

    await canonicalize_source(duplicate.id, db)

    assert db.flush_calls == 1
    assert canonical.is_duplicate_of is None
    assert duplicate.is_duplicate_of == canonical.id
    assert canonical.domain_reliability == DomainReliability.UNKNOWN
    assert canonical.authority_type == AuthorityType.SECONDARY
    assert duplicate.authority_type == AuthorityType.SECONDARY
    assert unrelated.is_duplicate_of is None
    assert unrelated.authority_type == AuthorityType.UNKNOWN


@pytest.mark.asyncio
async def test_canonicalize_source_propagates_extracted_date_to_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    long_preview = "Long enough preview for extraction. " * 6
    canonical = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://example.com/article",
        title="Canonical",
        text_preview=long_preview,
        created_at=datetime(2024, 1, 1),
        domain_reliability=DomainReliability.UNKNOWN,
        authority_type=AuthorityType.UNKNOWN,
        published_at=None,
    )
    duplicate = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://www.example.com/article",
        title="Duplicate",
        text_preview=long_preview,
        created_at=datetime(2024, 1, 2),
        domain_reliability=DomainReliability.UNKNOWN,
        authority_type=AuthorityType.UNKNOWN,
        published_at=None,
    )
    db = _FakeCanonicalizeDB(
        [
            _FakeScalarResult(scalar=duplicate),
            _FakeScalarResult(rows=[canonical, duplicate]),
        ]
    )

    async def _fake_classify(source):
        return AuthorityType.SECONDARY

    async def _fake_extract_date(source):
        return datetime(2024, 5, 1), "success", "extracted"

    monkeypatch.setattr(
        "app.ingestion.canonicalizer.classify_source_authority",
        _fake_classify,
    )
    monkeypatch.setattr(
        "app.ingestion.canonicalizer._extract_date",
        _fake_extract_date,
    )

    await canonicalize_source(duplicate.id, db)

    assert canonical.published_at == datetime(2024, 5, 1)
    assert duplicate.published_at == datetime(2024, 5, 1)
