import uuid
from datetime import datetime

import pytest

from app.ingestion.canonicalizer import (
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
