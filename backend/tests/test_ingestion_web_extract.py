import socket

import pytest

from app.ingestion.web import (
    DNSResolutionError,
    _dump_extractor_output,
    _extract_wikipedia_revision_date,
    _parse_wikipedia_page,
    _resolve_host_infos,
    _wikipedia_sections_to_markdown,
    extract_web,
)


@pytest.mark.asyncio
async def test_extract_web_falls_back_when_jina_returns_blocked_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._check_ssrf", lambda url: None)
    monkeypatch.setattr("app.ingestion.web.settings.jina_api_key", "test-key", raising=False)

    async def _fake_jina(url: str) -> tuple[str, str | None]:
        return (
            "Warning: Target URL returned error 403: Forbidden\n"
            "This page may require CAPTCHA.\n",
            "N/A",
        )

    async def _fake_httpx(url: str) -> tuple[str, str | None]:
        return ("Actual article body from httpx fallback.", "Real title")

    monkeypatch.setattr("app.ingestion.web._jina_extract", _fake_jina)
    monkeypatch.setattr("app.ingestion.web._httpx_extract", _fake_httpx)

    text, title, date = await extract_web("https://example.com/article")

    assert text == "Actual article body from httpx fallback."
    assert title == "Real title"


@pytest.mark.asyncio
async def test_extract_web_prefers_mediawiki_for_wikipedia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._check_ssrf", lambda url: None)

    async def _fake_mediawiki(url: str):
        return ("## History\n\nArticle body", "Hong Kong Monetary Authority", None)

    async def _should_not_call(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("Fallback extractors should not run when MediaWiki succeeds")

    monkeypatch.setattr("app.ingestion.web._wikipedia_api_extract", _fake_mediawiki)
    monkeypatch.setattr("app.ingestion.web._trafilatura_extract", _should_not_call)
    monkeypatch.setattr("app.ingestion.web._jina_extract", _should_not_call)
    monkeypatch.setattr("app.ingestion.web._httpx_extract", _should_not_call)

    text, title, date = await extract_web("https://en.wikipedia.org/wiki/Hong_Kong_Monetary_Authority")

    assert text.startswith("## History")
    assert title == "Hong Kong Monetary Authority"
    assert date is None


@pytest.mark.asyncio
async def test_extract_web_wikipedia_falls_back_to_trafilatura_when_mediawiki_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._check_ssrf", lambda url: None)

    async def _fake_mediawiki(url: str):
        return None

    async def _fake_trafilatura(url: str):
        return ("Trafilatura text", "Fallback title", "2025-01-01")

    monkeypatch.setattr("app.ingestion.web._wikipedia_api_extract", _fake_mediawiki)
    monkeypatch.setattr("app.ingestion.web._trafilatura_extract", _fake_trafilatura)

    text, title, date = await extract_web("https://en.wikipedia.org/wiki/Hong_Kong_Monetary_Authority")

    assert text == "Trafilatura text"
    assert title == "Fallback title"
    assert date == "2025-01-01"


@pytest.mark.asyncio
async def test_extract_web_wikipedia_falls_back_when_mediawiki_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._check_ssrf", lambda url: None)

    async def _fake_mediawiki(url: str):
        raise RuntimeError("mediawiki down")

    async def _fake_trafilatura(url: str):
        return ("Trafilatura text", "Fallback title", None)

    monkeypatch.setattr("app.ingestion.web._wikipedia_api_extract", _fake_mediawiki)
    monkeypatch.setattr("app.ingestion.web._trafilatura_extract", _fake_trafilatura)

    text, title, date = await extract_web("https://en.wikipedia.org/wiki/Hong_Kong_Monetary_Authority")

    assert text == "Trafilatura text"
    assert title == "Fallback title"
    assert date is None


def test_parse_wikipedia_page_accepts_article_paths() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Hong_Kong_Monetary_Authority")
    assert parsed == ("en.wikipedia.org", "Hong_Kong_Monetary_Authority")


def test_parse_wikipedia_page_rejects_namespace_paths() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Category:Hong_Kong")
    assert parsed is None


def test_parse_wikipedia_page_rejects_non_wikipedia_domains() -> None:
    parsed = _parse_wikipedia_page("https://notwikipedia.org/wiki/Foo")
    assert parsed is None


def test_parse_wikipedia_page_rejects_encoded_namespace_paths() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Category%3AHong_Kong")
    assert parsed is None


def test_parse_wikipedia_page_accepts_articles_with_colon_in_slug() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Star_Trek:_The_Next_Generation")
    assert parsed == ("en.wikipedia.org", "Star_Trek:_The_Next_Generation")


def test_extract_wikipedia_revision_date_from_first_revision() -> None:
    page_obj = {"revisions": [{"timestamp": "2004-04-26T12:34:56Z"}]}
    assert _extract_wikipedia_revision_date(page_obj) == "2004-04-26"


def test_extract_wikipedia_revision_date_returns_none_on_missing_revision() -> None:
    assert _extract_wikipedia_revision_date({}) is None
    assert _extract_wikipedia_revision_date({"revisions": []}) is None
    assert _extract_wikipedia_revision_date({"revisions": [{"timestamp": "invalid"}]}) is None


def test_dump_extractor_output_writes_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.ingestion.web.settings.web_extractor_debug_dump_dir",
        str(tmp_path),
        raising=False,
    )

    _dump_extractor_output(
        url="https://example.com/article",
        extractor="jina",
        status="success",
        text="hello world",
        title="Example",
        date="2026-02-25",
    )

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "extractor: jina" in content
    assert "status: success" in content
    assert "text_chars: 11" in content
    assert "hello world" in content


def test_dump_extractor_output_skips_when_disabled(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingestion.web.settings.web_extractor_debug_dump_dir", "", raising=False)
    _dump_extractor_output(
        url="https://example.com/article",
        extractor="trafilatura",
        status="success",
        text="data",
    )
    assert list(tmp_path.glob("*.md")) == []


def test_dump_extractor_output_resolves_relative_path_from_repo_root(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._REPO_ROOT", tmp_path)
    monkeypatch.setattr("app.ingestion.web.settings.web_extractor_debug_dump_dir", "debug/web", raising=False)

    _dump_extractor_output(
        url="https://example.com/article",
        extractor="httpx",
        status="success",
        text="payload",
    )

    files = list((tmp_path / "debug" / "web").glob("*.md"))
    assert len(files) == 1
    assert "extractor: httpx" in files[0].read_text(encoding="utf-8")


def test_resolve_host_infos_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def _fake_getaddrinfo(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise socket.gaierror(8, "temporary")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("app.ingestion.web.socket.getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr("app.ingestion.web.time.sleep", lambda _: None)
    monkeypatch.setattr("app.ingestion.web.settings.web_dns_resolve_retries", 3, raising=False)
    monkeypatch.setattr("app.ingestion.web.settings.web_dns_resolve_retry_delay_ms", 0, raising=False)

    infos = _resolve_host_infos("example.com")

    assert calls["count"] == 3
    assert infos


def test_resolve_host_infos_raises_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_getaddrinfo(*args, **kwargs):
        raise socket.gaierror(8, "not known")

    monkeypatch.setattr("app.ingestion.web.socket.getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr("app.ingestion.web.time.sleep", lambda _: None)
    monkeypatch.setattr("app.ingestion.web.settings.web_dns_resolve_retries", 2, raising=False)
    monkeypatch.setattr("app.ingestion.web.settings.web_dns_resolve_retry_delay_ms", 0, raising=False)

    with pytest.raises(DNSResolutionError):
        _resolve_host_infos("example.com")


def test_wikipedia_sections_to_markdown_maps_levels() -> None:
    text = "Intro\n== History ==\nText\n=== Institutions ===\nMore"
    converted = _wikipedia_sections_to_markdown(text)
    assert "## History" in converted
    assert "### Institutions" in converted
