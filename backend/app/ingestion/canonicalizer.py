"""Source canonicalization: deduplication, domain reliability, and date extraction."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ingestion.web_quality import contains_blocking_signal
from app.ingestion.source_metadata import canonical_url, classify_domain, classify_source_authority
from app.models.source import DomainReliability, Source, SourceType
from app.observability import safe_set_run, trace_span
from app.pipeline.llm_json import parse_llm_json
from app.services.llm import call_haiku_structured

logger = logging.getLogger(__name__)

# --- Main pipeline ------------------------------------------------------------

async def canonicalize_sources(project_id: str, db: AsyncSession) -> None:
    """Dedup and classify all sources in a project.

    Steps:
      1. URL canonical dedup -- mark duplicates via is_duplicate_of.
      2. Domain reliability classification.
      3. Date extraction using Haiku (for web sources missing published_at).
    """
    result = await db.execute(
        select(Source).where(
            Source.project_id == project_id,
            Source.is_duplicate_of.is_(None),
        )
    )
    sources = list(result.scalars().all())

    if not sources:
        return

    # --- Step 1: Dedup by canonical URL ----------------------------------------
    seen_urls: dict[str, Source] = {}

    for source in sources:
        if source.source_type == SourceType.WEB and source.original_url:
            canon = canonical_url(source.original_url)
            if canon in seen_urls:
                source.is_duplicate_of = seen_urls[canon].id
                logger.info(
                    "Source %s is duplicate of %s (canonical: %s)",
                    source.id,
                    seen_urls[canon].id,
                    canon,
                )
            else:
                seen_urls[canon] = source

    # --- Step 2: Domain reliability + authority --------------------------------
    for source in sources:
        if source.is_duplicate_of:
            continue

        if source.original_url:
            source.domain_reliability = classify_domain(source.original_url)
        elif source.source_type == SourceType.PDF:
            source.domain_reliability = DomainReliability.MEDIUM
        source.authority_type = await classify_source_authority(source)

    # --- Step 3: Extract published_at with Haiku (for undated web sources) -----
    undated = [s for s in sources if s.published_at is None and not s.is_duplicate_of and s.source_type == SourceType.WEB]

    for source in undated:
        preview_chars = len((source.text_preview or "").strip())
        can_extract, reason = _date_extraction_gate(
            source,
            min_chars=settings.web_date_extraction_min_preview_chars,
        )

        with trace_span(
            "ingestion.web_quality.gate_c",
            run_type="tool",
            inputs={
                "source_id": str(source.id),
                "preview_chars": preview_chars,
                "web_quality_state": _source_web_quality_state(source),
                "min_chars": settings.web_date_extraction_min_preview_chars,
            },
        ) as gate_run:
            safe_set_run(
                gate_run,
                outputs={
                    "eligible": can_extract,
                    "reason": reason,
                    "skipped": not can_extract,
                },
            )

        if not can_extract:
            _set_date_extraction_metadata(
                source,
                status="skipped",
                reason=reason,
                preview_chars=preview_chars,
            )
            continue

        try:
            with trace_span(
                "ingestion.canonicalizer.extract_published_at",
                run_type="llm",
                inputs={"source_id": str(source.id), "preview_chars": preview_chars},
            ) as extract_run:
                published_at, status, status_reason = await _extract_date(source)
                source.published_at = published_at
                _set_date_extraction_metadata(
                    source,
                    status=status,
                    reason=status_reason,
                    preview_chars=preview_chars,
                )
                safe_set_run(
                    extract_run,
                    outputs={
                        "status": status,
                        "reason": status_reason,
                        "published_at": str(published_at) if published_at else None,
                    },
                )
        except Exception:
            _set_date_extraction_metadata(
                source,
                status="error",
                reason="llm_call_failed",
                preview_chars=preview_chars,
            )
            logger.warning("Date extraction failed for source %s", source.id, exc_info=True)

    await db.flush()
    logger.info("Canonicalized %d sources for project %s", len(sources), project_id)


def _source_web_quality_state(source: Source) -> str | None:
    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
    web_quality = metadata.get("web_quality")
    if isinstance(web_quality, dict):
        state = web_quality.get("state")
        return str(state) if state else None
    return None


def _date_extraction_gate(source: Source, *, min_chars: int) -> tuple[bool, str]:
    preview = (source.text_preview or "").strip()
    if not preview:
        return False, "empty_preview"

    state = _source_web_quality_state(source)
    if state == "blocked":
        return False, "blocked_by_gate_a"

    if contains_blocking_signal(preview):
        return False, "blocked_signal_in_preview"

    if len(preview) < min_chars:
        return False, "preview_too_short"

    return True, "eligible"


def _set_date_extraction_metadata(
    source: Source,
    *,
    status: str,
    reason: str,
    preview_chars: int,
) -> None:
    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
    metadata = dict(metadata)
    metadata["date_extraction"] = {
        "status": status,
        "reason": reason,
        "preview_chars": preview_chars,
    }
    source.metadata_ = metadata


async def _extract_date(source: Source) -> tuple[datetime | None, str, str]:
    """Use Haiku to extract a publication date from source metadata and text preview."""
    prompt = (
        f"Title: {source.title or 'N/A'}\n"
        f"URL: {source.original_url or 'N/A'}\n"
        f"Text preview (first 500 chars):\n{source.text_preview or ''}\n\n"
        "Extract the publication date of this content. "
        "Return JSON: {\"date\": \"YYYY-MM-DD\"} or {\"date\": null} if not found."
    )

    raw = await call_haiku_structured(
        system="You are a metadata extractor. Extract publication dates from content.",
        prompt=prompt,
        max_tokens=64,
        temperature=0.0,
    )

    try:
        data = parse_llm_json(raw)
    except ValueError:
        logger.debug("Could not parse date response JSON: %s", raw)
        return None, "parse_error", "invalid_json"

    if not isinstance(data, dict):
        return None, "parse_error", "not_an_object"

    date_str = data.get("date")
    if not date_str:
        return None, "not_found", "empty_or_null_date"

    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d"), "success", "extracted"
    except ValueError:
        logger.debug("Could not parse date format: %s", date_str)
        return None, "parse_error", "invalid_date_format"
