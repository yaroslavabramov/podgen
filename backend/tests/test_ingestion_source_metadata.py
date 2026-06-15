import uuid

import pytest

from app.ingestion.source_metadata import (
    canonical_url,
    classify_authority_heuristic,
    classify_domain,
    classify_source_authority,
)
from app.models.source import AuthorityType, DomainReliability, Source, SourceType


def _make_source(
    *,
    url: str | None,
    title: str | None = None,
    text_preview: str | None = None,
    source_type: str = SourceType.WEB,
) -> Source:
    return Source(
        project_id=uuid.uuid4(),
        source_type=source_type,
        original_url=url,
        title=title,
        text_preview=text_preview,
    )


def test_canonical_url_strips_scheme_and_www() -> None:
    assert canonical_url("https://www.example.com/path/?utm=1") == "example.com/path"


def test_classify_domain_government_is_high() -> None:
    assert classify_domain("https://www.gov.hk/en/residents/") == DomainReliability.HIGH


def test_classify_authority_heuristic_primary_for_regulator_domain() -> None:
    authority = classify_authority_heuristic(
        source_type=SourceType.WEB,
        original_url="https://www.federalreserve.gov/newsevents/pressreleases.htm",
        title="Press release",
        text_preview=None,
    )
    assert authority == AuthorityType.PRIMARY


def test_classify_authority_heuristic_secondary_for_reuters() -> None:
    authority = classify_authority_heuristic(
        source_type=SourceType.WEB,
        original_url="https://www.reuters.com/world/article",
        title="Analysis: Market pressure eases",
        text_preview=None,
    )
    assert authority == AuthorityType.SECONDARY


@pytest.mark.asyncio
async def test_classify_source_authority_uses_llm_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call(**kwargs) -> str:
        return '{"authority_type": "tertiary"}'

    monkeypatch.setattr("app.ingestion.source_metadata.call_haiku_structured", _fake_call)
    source = _make_source(
        url="https://example.com/article",
        title="Overview of the system",
        text_preview="A summary article with no obvious primary markers.",
    )

    authority = await classify_source_authority(source)

    assert authority == AuthorityType.TERTIARY


@pytest.mark.asyncio
async def test_classify_source_authority_prompt_limits_external_knowledge(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def _fake_call(**kwargs) -> str:
        captured["system"] = kwargs["system"]
        captured["prompt"] = kwargs["prompt"]
        return '{"authority_type": "unknown"}'

    monkeypatch.setattr("app.ingestion.source_metadata.call_haiku_structured", _fake_call)
    source = _make_source(
        url="https://example.com/article",
        title="Ambiguous note",
        text_preview="A preview with no official markers.",
    )

    authority = await classify_source_authority(source)

    assert authority == AuthorityType.UNKNOWN
    assert "Use only the provided URL, title, preview, and metadata." in captured["system"]
    assert "Do not use outside knowledge." in captured["system"]
    assert "If the provided fields are insufficient, return unknown." in captured["system"]
