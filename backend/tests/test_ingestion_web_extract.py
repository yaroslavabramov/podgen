import pytest

from app.ingestion.web import extract_web


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

    text, title = await extract_web("https://example.com/article")

    assert text == "Actual article body from httpx fallback."
    assert title == "Real title"
