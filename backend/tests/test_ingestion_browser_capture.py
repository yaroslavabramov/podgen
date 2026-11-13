from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from podgen.sources.acquisition import browser
from podgen.sources.acquisition.contracts import AcquisitionProfile
from podgen.sources.acquisition.http import ByteLimitExceeded


@pytest.mark.parametrize("limit", [5, 1000])
@pytest.mark.asyncio
async def test_rendered_dom_has_its_own_bytes_metadata_and_closes_browser(monkeypatch, limit):
    closed = []
    body = "<html><body>Saved rendered DOM</body></html>"

    class Page:
        url = "https://example.com/article"

        async def content(self):
            return body

    class Response:
        status = 403

        async def all_headers(self):
            return {"etag": "original-server-tag", "content-encoding": "gzip", "set-cookie": "secret"}

    class Browser:
        async def new_context(self, **options):
            assert options["service_workers"] == "block"
            return self

        async def new_page(self):
            return Page()

        async def close(self):
            closed.append(True)

    async def launch(**kwargs):
        return Browser()

    @asynccontextmanager
    async def engine():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    async def goto(*args, **kwargs):
        return Response()

    monkeypatch.setattr(browser, "guarded_page_goto", goto)
    monkeypatch.setattr(browser, "public_browser_launch_args", lambda url: [])
    if limit == 5:
        with pytest.raises(ByteLimitExceeded, match="rendered_dom"):
            await browser.BrowserSourceFetcher()._capture(
                Page.url, AcquisitionProfile(max_bytes=limit), engine
            )
    else:
        result = await browser.BrowserSourceFetcher()._capture(
            Page.url, AcquisitionProfile(max_bytes=limit), engine
        )
        assert result.body == body.encode() and result.status_code == 403
        assert result.acquisition_kind == "browser_snapshot"
        assert "content-encoding" not in result.headers and "set-cookie" not in result.headers
        assert result.headers["etag"] == "original-server-tag"
    assert closed == [True]
