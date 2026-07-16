from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.safe_browser import guarded_page_goto, public_browser_launch_args
from app.generation_v2.source_content_readiness import (
    assess_web_source_content,
    apply_redirect_target_guard,
    content_assessment_metadata,
    status_for_content_assessment,
)
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
        content_type = response.headers.get("content-type", "")
        assessment = assess_web_source_content(
            html,
            surface="workspace_fetched_html",
            http_status=response.status_code,
            content_type=content_type,
            input_is_markup=True,
        )
        assessment = apply_redirect_target_guard(
            assessment,
            requested_url=source.original_url,
            final_url=str(response.url),
        )
        object_name = f"v2/sources/{source.id}/fetched.html"
        upload_text(get_minio_client(), html, object_name)
        rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.FETCHED_HTML,
            storage_path=object_name,
            status=status_for_content_assessment(assessment),
            content=html,
            metadata={
                "url": str(response.url),
                "requested_url": source.original_url,
                "status_code": response.status_code,
                "content_type": content_type,
                "canonical": assessment.confirmed,
                "content_assessment": content_assessment_metadata(assessment),
            },
            error_message=None if assessment.confirmed else ", ".join(assessment.reasons),
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
            browser = await playwright.chromium.launch(args=public_browser_launch_args(source.original_url))
            try:
                context = await browser.new_context(service_workers="block")
                page = await context.new_page()
                navigation_response = await guarded_page_goto(
                    page,
                    source.original_url,
                    wait_until="networkidle",
                    timeout=settings.web_fetch_timeout * 1000,
                )
                text = await page.locator("body").inner_text(timeout=5000)
                final_url = str(page.url)
            finally:
                await browser.close()

        status_code = _browser_response_status(navigation_response)
        content_type = await _browser_response_content_type(navigation_response)
        assessment = assess_web_source_content(
            text,
            surface="workspace_rendered_snapshot",
            http_status=status_code,
            content_type=content_type,
            input_is_markup=False,
        )
        assessment = apply_redirect_target_guard(
            assessment,
            requested_url=source.original_url,
            final_url=final_url,
        )
        object_name = f"v2/sources/{source.id}/rendered.txt"
        upload_text(get_minio_client(), text, object_name)
        return await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
            storage_path=object_name,
            status=status_for_content_assessment(assessment),
            content=text,
            metadata={
                "url": final_url,
                "requested_url": source.original_url,
                "canonical": assessment.confirmed,
                "format": "rendered_text",
                "status_code": status_code,
                "content_type": content_type,
                "content_assessment": content_assessment_metadata(assessment),
            },
            error_message=None if assessment.confirmed else ", ".join(assessment.reasons),
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


def _browser_response_status(response) -> int | None:
    value = getattr(response, "status", None) if response is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _browser_response_content_type(response) -> str:
    if response is None:
        return ""
    headers = getattr(response, "headers", {}) or {}
    if callable(headers):
        headers = await headers()
    if isinstance(headers, dict):
        return str(headers.get("content-type") or headers.get("Content-Type") or "")
    return ""

__all__ = ["capture_rendered_snapshot", "capture_web_representations"]
