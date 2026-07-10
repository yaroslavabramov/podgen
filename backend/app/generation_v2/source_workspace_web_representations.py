from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.generation_v2 import SourceRepresentation, SourceRepresentationKind, V2RecordStatus
from app.models.source import Source
from app.services.storage import get_minio_client, upload_text

logger = logging.getLogger(__name__)


async def _save_representation(*args, **kwargs) -> SourceRepresentation:
    from app.generation_v2.source_workspace_representations import save_representation

    return await save_representation(*args, **kwargs)


async def capture_web_representations(db: AsyncSession, source: Source) -> list[uuid.UUID]:
    if not source.original_url:
        return []

    reps: list[uuid.UUID] = []
    try:
        from app.ingestion.web import _safe_fetch

        response = await _safe_fetch(source.original_url)
        html = response.text
        object_name = f"v2/sources/{source.id}/fetched.html"
        upload_text(get_minio_client(), html, object_name)
        rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.FETCHED_HTML,
            storage_path=object_name,
            status=V2RecordStatus.READY,
            content=html,
            metadata={
                "url": str(response.url),
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "canonical": True,
            },
        )
        reps.append(rep.id)
    except Exception as exc:
        rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.FETCHED_HTML,
            storage_path=None,
            status=V2RecordStatus.FAILED,
            metadata={"url": source.original_url},
            error_message=str(exc),
        )
        reps.append(rep.id)

    if settings.generation_v2_enable_rendered_snapshots:
        reps.append((await capture_rendered_snapshot(db, source)).id)

    return reps


async def capture_rendered_snapshot(db: AsyncSession, source: Source) -> SourceRepresentation:
    if not source.original_url:
        return await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
            status=V2RecordStatus.FAILED,
            metadata={},
            error_message="Source has no URL",
        )
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.goto(source.original_url, wait_until="networkidle", timeout=settings.web_fetch_timeout * 1000)
            text = await page.locator("body").inner_text(timeout=5000)
            await browser.close()

        object_name = f"v2/sources/{source.id}/rendered.txt"
        upload_text(get_minio_client(), text, object_name)
        return await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
            storage_path=object_name,
            status=V2RecordStatus.READY,
            content=text,
            metadata={"url": source.original_url, "canonical": True, "format": "rendered_text"},
        )
    except Exception as exc:
        logger.info("Rendered snapshot unavailable for source %s: %s", source.id, exc)
        return await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
            status=V2RecordStatus.FAILED,
            metadata={"url": source.original_url},
            error_message=str(exc),
        )

__all__ = ["capture_rendered_snapshot", "capture_web_representations"]
