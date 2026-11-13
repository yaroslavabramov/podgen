"""Optional rendered DOM snapshot with the ported browser network controls."""

import asyncio

from podgen.persistence.repository import timestamp

from .browser_guard import guarded_page_goto, public_browser_launch_args
from .contracts import FetchedDocument
from .http import ByteLimitExceeded


class BrowserSourceFetcher:
    async def fetch(self, url, profile, *, headers=None):
        from playwright.async_api import Error, async_playwright

        try:
            return await self._capture(url, profile, async_playwright)
        except Error as error:
            raise RuntimeError("browser_capture_failed:" + str(error)) from error

    async def _capture(self, url, profile, async_playwright):
        async with asyncio.timeout(profile.timeout_seconds):
            async with async_playwright() as engine:
                browser = await engine.chromium.launch(headless=True, args=public_browser_launch_args(url))
                try:
                    context = await browser.new_context(
                        service_workers="block", accept_downloads=False, user_agent=profile.user_agent
                    )
                    page = await context.new_page()
                    response = await guarded_page_goto(
                        page,
                        url,
                        wait_until="domcontentloaded",
                        timeout=round(profile.timeout_seconds * 1000),
                    )
                    body = (await page.content()).encode("utf-8")
                    if len(body) > profile.max_bytes:
                        raise ByteLimitExceeded("source_rendered_dom_byte_limit")
                    status = response.status if response else 200
                    headers = await response.all_headers() if response else {}
                    headers = {
                        k.lower(): v
                        for k, v in headers.items()
                        if k.lower() not in ("set-cookie", "content-encoding", "content-length")
                    }
                    headers["content-type"] = "text/html; charset=utf-8"
                    return FetchedDocument(
                        url,
                        str(page.url),
                        [],
                        status,
                        headers,
                        body,
                        timestamp(),
                        acquisition_kind="browser_snapshot",
                        limitations=[
                            "rendered_dom_snapshot_not_http_original",
                            "cross_origin_assets_and_service_workers_blocked",
                        ],
                    )
                finally:
                    await browser.close()
