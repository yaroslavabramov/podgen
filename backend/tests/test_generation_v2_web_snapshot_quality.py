from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.generation_v2.source_corpus_web_snapshot import ensure_web_snapshot_versions
from app.models.generation_v2 import SourceRepresentationKind, V2RecordStatus
from app.models.source import Source, SourceType


class _Response:
    def __init__(
        self,
        text: str,
        *,
        status_code: int = 200,
        content_type: str = "text/html",
        url: str = "https://example.com/article",
        content: bytes | None = None,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = url
        self.content = content if content is not None else text.encode()


def _source() -> Source:
    return Source(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        original_url="https://example.com/article",
    )


def _capture_version_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def ensure_version(_db, source, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            source_id=source.id,
            kind=kwargs["kind"],
            status=kwargs.get("status", V2RecordStatus.READY),
            metadata_=kwargs["metadata"],
        )

    monkeypatch.setattr(
        "app.generation_v2.source_corpus_web_snapshot.ensure_source_version",
        ensure_version,
    )
    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.get_minio_client", lambda: object())
    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.upload_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.upload_file", lambda *_args, **_kwargs: None)
    return calls


@pytest.mark.asyncio
async def test_cookie_only_direct_capture_is_persisted_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_version_calls(monkeypatch)
    response = _Response(
        """We use cookies that are necessary to make our site work.
        Manage your cookie preferences using Cookie settings.
        Cookie settings
        Accept only necessary cookies
        Accept all cookies"""
    )

    async def fetch(_url):
        return response

    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.fetch_url_for_v2_snapshot", fetch)
    monkeypatch.setattr(settings, "generation_v2_enable_rendered_snapshots", False)

    versions = await ensure_web_snapshot_versions(SimpleNamespace(), _source())  # type: ignore[arg-type]

    assert len(versions) == 1
    assert calls[0]["kind"] == SourceRepresentationKind.FETCHED_HTML
    assert calls[0]["status"] == V2RecordStatus.FAILED
    assert calls[0]["metadata"]["canonical"] is False
    assert calls[0]["metadata"]["content_assessment"]["state"] == "unavailable"
    assert "cookie_consent_only" in calls[0]["metadata"]["content_assessment"]["reasons"]


@pytest.mark.asyncio
async def test_valid_direct_capture_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_version_calls(monkeypatch)
    response = _Response(
        "A detailed report explains the history, operating mechanism, evidence, consequences, and limits. " * 20,
        content_type="text/plain",
    )

    async def fetch(_url):
        return response

    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.fetch_url_for_v2_snapshot", fetch)
    monkeypatch.setattr(settings, "generation_v2_enable_rendered_snapshots", False)

    await ensure_web_snapshot_versions(SimpleNamespace(), _source())  # type: ignore[arg-type]

    assert calls[0]["status"] == V2RecordStatus.READY
    assert calls[0]["metadata"]["canonical"] is True
    assert calls[0]["metadata"]["content_assessment"]["state"] == "confirmed"
    assert calls[1]["kind"] == SourceRepresentationKind.EXTRACTED_TEXT
    assert calls[1]["status"] == V2RecordStatus.READY
    assert calls[1]["metadata"]["view"] == "fetched_document_text"


@pytest.mark.asyncio
async def test_pdf_content_type_with_html_body_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_version_calls(monkeypatch)
    response = _Response(
        "<html><body>Access denied</body></html>",
        content_type="application/pdf",
    )

    async def fetch(_url):
        return response

    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.fetch_url_for_v2_snapshot", fetch)
    monkeypatch.setattr(settings, "generation_v2_enable_rendered_snapshots", False)

    await ensure_web_snapshot_versions(SimpleNamespace(), _source())  # type: ignore[arg-type]

    pdf_call = next(call for call in calls if call["kind"] == SourceRepresentationKind.ORIGINAL_FILE)
    assert pdf_call["status"] == V2RecordStatus.FAILED
    assert pdf_call["metadata"]["canonical"] is False
    assert pdf_call["metadata"]["content_assessment"]["reasons"] == ["invalid_pdf_signature"]


class _BrowserPage:
    url = "https://example.com/article"

    def __init__(self, text: str) -> None:
        self.text = text
        self.context = self

    def locator(self, _selector: str):
        return self

    async def inner_text(self, *, timeout: int) -> str:
        del timeout
        return self.text


class _BrowserContext:
    def __init__(self, page: _BrowserPage) -> None:
        self.page = page

    async def new_page(self) -> _BrowserPage:
        return self.page


class _Browser:
    def __init__(self, page: _BrowserPage) -> None:
        self.page = page

    async def new_context(self, **_kwargs) -> _BrowserContext:
        return _BrowserContext(self.page)

    async def close(self) -> None:
        return None


class _Chromium:
    def __init__(self, page: _BrowserPage) -> None:
        self.page = page

    async def launch(self, **_kwargs) -> _Browser:
        return _Browser(self.page)


class _PlaywrightContextManager:
    def __init__(self, page: _BrowserPage) -> None:
        self.playwright = SimpleNamespace(chromium=_Chromium(page))

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
async def test_rendered_http_403_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_version_calls(monkeypatch)
    page = _BrowserPage(
        "403 ERROR\nThe request could not be satisfied.\nGenerated by cloudfront (CloudFront)\n"
        "Request ID: tzWL_P-TH7fqtwA8sLlcagavIdcXnDKe4FJWyy5USftLf7eqq2bKZw=="
    )

    async def fail_fetch(_url):
        raise RuntimeError("direct fetch unavailable")

    async def goto(*_args, **_kwargs):
        return SimpleNamespace(status=403, headers={"content-type": "text/html"})

    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.fetch_url_for_v2_snapshot", fail_fetch)
    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.public_browser_launch_args", lambda _url: [])
    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.guarded_page_goto", goto)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _PlaywrightContextManager(page))
    monkeypatch.setattr(settings, "generation_v2_enable_rendered_snapshots", True)

    await ensure_web_snapshot_versions(SimpleNamespace(), _source())  # type: ignore[arg-type]

    assert len(calls) == 1
    assert calls[0]["kind"] == SourceRepresentationKind.RENDERED_SNAPSHOT
    assert calls[0]["status"] == V2RecordStatus.FAILED
    assert calls[0]["metadata"]["canonical"] is False
    assert calls[0]["metadata"]["status_code"] == 403
    assert "http_status_403" in calls[0]["metadata"]["content_assessment"]["reasons"]
    assert "cloudfront_error_page" in calls[0]["metadata"]["content_assessment"]["reasons"]


@pytest.mark.asyncio
async def test_rendered_http_403_with_substantive_body_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_version_calls(monkeypatch)
    page = _BrowserPage(
        "A detailed report explains the history, operating mechanism, evidence, consequences, and limits. " * 20
    )

    async def fail_fetch(_url):
        raise RuntimeError("direct fetch unavailable")

    async def goto(*_args, **_kwargs):
        return SimpleNamespace(status=403, headers={"content-type": "text/html"})

    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.fetch_url_for_v2_snapshot", fail_fetch)
    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.public_browser_launch_args", lambda _url: [])
    monkeypatch.setattr("app.generation_v2.source_corpus_web_snapshot.guarded_page_goto", goto)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _PlaywrightContextManager(page))
    monkeypatch.setattr(settings, "generation_v2_enable_rendered_snapshots", True)

    await ensure_web_snapshot_versions(SimpleNamespace(), _source())  # type: ignore[arg-type]

    assert len(calls) == 1
    assert calls[0]["status"] == V2RecordStatus.READY
    assert calls[0]["metadata"]["canonical"] is True
    assert calls[0]["metadata"]["content_assessment"]["reasons"] == ["http_status_403"]
