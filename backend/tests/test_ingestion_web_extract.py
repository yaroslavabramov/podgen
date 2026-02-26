import socket

import pytest

from app.ingestion.web import (
    DNSResolutionError,
    _dump_extractor_output,
    _extract_jina_title,
    _extract_wikipedia_revision_date,
    _find_pdf_candidate_url_from_markdown,
    _is_navigation_heavy_jina_text,
    _looks_like_pdf_url,
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
async def test_extract_web_direct_pdf_url_uses_pdf_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._check_ssrf", lambda url: None)
    monkeypatch.setattr("app.ingestion.web._looks_like_pdf_url", lambda url: True)

    async def _fake_pdf_extract(url: str):
        return ("PDF body content", "paper", None)

    async def _should_not_call(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("Cascade extractors should not run when direct PDF succeeds")

    monkeypatch.setattr("app.ingestion.web._extract_pdf_from_url", _fake_pdf_extract)
    monkeypatch.setattr("app.ingestion.web._wikipedia_api_extract", _should_not_call)
    monkeypatch.setattr("app.ingestion.web._trafilatura_extract", _should_not_call)
    monkeypatch.setattr("app.ingestion.web._jina_extract", _should_not_call)
    monkeypatch.setattr("app.ingestion.web._httpx_extract", _should_not_call)

    text, title, date = await extract_web("https://example.com/paper.pdf")

    assert text == "PDF body content"
    assert title == "paper"
    assert date is None


@pytest.mark.asyncio
async def test_extract_web_promotes_pdf_from_navigation_heavy_jina(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._check_ssrf", lambda url: None)
    monkeypatch.setattr("app.ingestion.web.settings.jina_api_key", "test-key", raising=False)

    async def _fake_trafilatura(url: str):
        return None

    jina_text = (
        "Published Time: 1951-01-01\n"
        "###### Topics\n"
        "[Many Link](https://example.com/a)\n"
        "[Download PDF](https://example.com/downloadpdf/view/paper.pdf)\n"
    )

    async def _fake_jina(url: str) -> tuple[str, str | None]:
        return (jina_text, "Portal title")

    async def _fake_pdf_extract(url: str):
        assert "downloadpdf" in url
        return ("PDF promoted body", "Promoted PDF", None)

    async def _should_not_call(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("httpx fallback should not run when promoted PDF succeeds")

    monkeypatch.setattr("app.ingestion.web._trafilatura_extract", _fake_trafilatura)
    monkeypatch.setattr("app.ingestion.web._jina_extract", _fake_jina)
    monkeypatch.setattr("app.ingestion.web._find_pdf_candidate_url_from_markdown", lambda text, base_url: "https://example.com/downloadpdf/view/paper.pdf")
    monkeypatch.setattr("app.ingestion.web._is_navigation_heavy_jina_text", lambda text: True)
    monkeypatch.setattr("app.ingestion.web._extract_pdf_from_url", _fake_pdf_extract)
    monkeypatch.setattr("app.ingestion.web._httpx_extract", _should_not_call)

    text, title, date = await extract_web("https://example.com/article")

    assert text == "PDF promoted body"
    assert title == "Promoted PDF"
    assert date == "1951-01-01"


@pytest.mark.asyncio
async def test_extract_web_keeps_jina_when_not_navigation_heavy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ingestion.web._check_ssrf", lambda url: None)
    monkeypatch.setattr("app.ingestion.web.settings.jina_api_key", "test-key", raising=False)

    async def _fake_trafilatura(url: str):
        return None

    jina_text = (
        "Published Time: 1951-01-01\n"
        "Core article text\n"
        "[Download PDF](https://example.com/downloadpdf/view/paper.pdf)\n"
    )

    async def _fake_jina(url: str) -> tuple[str, str | None]:
        return (jina_text, "Portal title")

    async def _should_not_call(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("PDF promotion should not run when page is not nav-heavy")

    monkeypatch.setattr("app.ingestion.web._trafilatura_extract", _fake_trafilatura)
    monkeypatch.setattr("app.ingestion.web._jina_extract", _fake_jina)
    monkeypatch.setattr("app.ingestion.web._is_navigation_heavy_jina_text", lambda text: False)
    monkeypatch.setattr("app.ingestion.web._extract_pdf_from_url", _should_not_call)

    text, title, date = await extract_web("https://example.com/article")

    assert text == jina_text
    assert title == "Portal title"
    assert date == "1951-01-01"


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


def test_extract_jina_title_uses_setext_heading_when_title_line_missing() -> None:
    payload = (
        "Published Time: 1951-01-01\n"
        "Open external link\n"
        "==================\n"
        "Evolution of the Colonial Sterling Exchange Standard\n"
        "====================================================\n"
        "Body starts here.\n"
    )

    assert _extract_jina_title(payload) == "Evolution of the Colonial Sterling Exchange Standard"


def test_extract_jina_title_uses_setext_when_title_line_is_na() -> None:
    payload = (
        "Title: N/A\n"
        "Lessons from history - HSBC History\n"
        "===================================\n"
        "Main body.\n"
    )

    assert _extract_jina_title(payload) == "Lessons from history - HSBC History"


def test_extract_jina_title_keeps_explicit_valid_title() -> None:
    payload = (
        "Title: Hong Kong Monetary Authority\n"
        "## Heading\n"
        "Body.\n"
    )

    assert _extract_jina_title(payload) == "Hong Kong Monetary Authority"


def test_extract_jina_title_fallback_skips_raw_title_metadata_line() -> None:
    payload = (
        "Title: N/A\n"
        "Body content starts here.\n"
    )

    assert _extract_jina_title(payload) == "Body content starts here."


def test_extract_jina_title_allows_legit_search_prefixed_heading() -> None:
    payload = (
        "Search Engine Market Structure\n"
        "==============================\n"
        "Body.\n"
    )

    assert _extract_jina_title(payload) == "Search Engine Market Structure"


def test_dump_extractor_output_writes_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.ingestion.web.settings.web_extractor_debug_dump_dir",
        str(tmp_path),
        raising=False,
    )

    _dump_extractor_output(
        url="https://example.com/article",
        extractor="jina",
        status="error",
        text="hello world",
        title="Example",
        date="2026-02-25",
        error="boom",
        error_type="RuntimeError",
    )

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "extractor: jina" in content
    assert "status: error" in content
    assert "text_chars: 11" in content
    assert "error: boom" in content
    assert "error_type: RuntimeError" in content
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


def test_looks_like_pdf_url_recognizes_pdf_and_downloadpdf_paths() -> None:
    assert _looks_like_pdf_url("https://example.com/paper.pdf")
    assert _looks_like_pdf_url("https://example.com/downloadpdf/view/journals/abc")
    assert not _looks_like_pdf_url("https://example.com/article")


def test_find_pdf_candidate_url_from_markdown_same_host_only() -> None:
    text = (
        "[Download PDF](https://example.com/downloadpdf/paper.pdf)\n"
        "[Other PDF](https://evil.test/paper.pdf)\n"
    )
    found = _find_pdf_candidate_url_from_markdown(text, base_url="https://example.com/article")
    assert found == "https://example.com/downloadpdf/paper.pdf"


def test_navigation_heavy_jina_text_detects_portal_like_markdown() -> None:
    text = "\n".join(
        [
            "###### Topics",
            "###### Countries",
            "Advanced search",
        ]
        + [f"[Link {i}](https://example.com/{i})" for i in range(60)]
    )
    assert _is_navigation_heavy_jina_text(text) is True
