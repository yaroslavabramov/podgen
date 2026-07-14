from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.source_corpus_helpers import (
    filename_from_url as _filename_from_url,
    hash_bytes as _hash_bytes,
    hash_text as _hash_text,
    is_pdf_response as _is_pdf_response,
    versioned_object_name as _versioned_object_name,
)
from app.generation_v2.source_corpus_version_persistence import ensure_source_version
from app.generation_v2.safe_browser import guarded_page_goto, public_browser_launch_args
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion
from app.models.source import Source
from app.services.storage import get_minio_client, upload_file, upload_text

logger = logging.getLogger(__name__)


async def ensure_web_snapshot_versions(db: AsyncSession, source: Source) -> list[SourceVersion]:
    versions: list[SourceVersion] = []
    if not source.original_url:
        return versions
    try:
        response = await fetch_url_for_v2_snapshot(source.original_url)
        content_type = response.headers.get("content-type", "")
        if _is_pdf_response(source.original_url, content_type, response.content):
            content_hash = _hash_bytes(response.content)
            filename = _filename_from_url(str(response.url), default=f"{source.id}.pdf", required_suffix=".pdf")
            object_name = _versioned_object_name(source.id, content_hash, filename)
            upload_file(get_minio_client(), response.content, object_name, content_type or "application/pdf")
            versions.append(
                await ensure_source_version(
                    db,
                    source,
                    kind=SourceRepresentationKind.ORIGINAL_FILE,
                    storage_path=object_name,
                    content_hash=content_hash,
                    metadata={
                        "canonical": True,
                        "source_type": "pdf",
                        "original_filename": filename,
                        "native_upload": True,
                        "fetched_from_url": True,
                        "url": str(response.url),
                        "status_code": response.status_code,
                        "content_type": content_type,
                    },
                )
            )
            return versions
        html = response.text
        content_hash = _hash_text(html)
        object_name = _versioned_object_name(source.id, content_hash, "fetched.html")
        upload_text(get_minio_client(), html, object_name)
        versions.append(
            await ensure_source_version(
                db,
                source,
                kind=SourceRepresentationKind.FETCHED_HTML,
                storage_path=object_name,
                content_hash=content_hash,
                metadata={
                    "canonical": True,
                    "source_type": "web",
                    "native_upload": True,
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "content_type": content_type,
                },
            )
        )
    except Exception as exc:
        logger.info("Fetched HTML source version unavailable for %s: %s", source.id, exc)
    if settings.generation_v2_enable_rendered_snapshots:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(args=public_browser_launch_args(source.original_url))
                try:
                    context = await browser.new_context(service_workers="block")
                    page = await context.new_page()
                    await guarded_page_goto(
                        page,
                        source.original_url,
                        wait_until="networkidle",
                        timeout=settings.web_fetch_timeout * 1000,
                    )
                    text = await page.locator("body").inner_text(timeout=5000)
                finally:
                    await browser.close()
            content_hash = _hash_text(text)
            object_name = _versioned_object_name(source.id, content_hash, "rendered.txt")
            upload_text(get_minio_client(), text, object_name)
            versions.append(
                await ensure_source_version(
                    db,
                    source,
                    kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
                    storage_path=object_name,
                    content_hash=content_hash,
                    metadata={"canonical": True, "url": source.original_url, "format": "rendered_text"},
                )
            )
        except Exception as exc:
            logger.info("Rendered source version unavailable for %s: %s", source.id, exc)
    return versions


async def fetch_url_for_v2_snapshot(url: str):
    from app.ingestion.web import _check_ssrf, _safe_fetch

    timeout_seconds = max(1, settings.web_fetch_timeout + 5)
    _check_ssrf(url)
    try:
        return await asyncio.wait_for(_safe_fetch(url), timeout=timeout_seconds)
    except Exception as first_exc:
        browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
            "Accept-Language": settings.web_accept_language,
        }
        try:
            return await asyncio.wait_for(_safe_fetch(url, headers=browser_headers), timeout=timeout_seconds)
        except Exception:
            raise first_exc


__all__ = ["ensure_web_snapshot_versions", "fetch_url_for_v2_snapshot"]
