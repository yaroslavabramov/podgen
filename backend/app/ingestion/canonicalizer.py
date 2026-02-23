"""Source canonicalization: deduplication, domain reliability, and date extraction."""

import json
import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source import DomainReliability, Source, SourceType
from app.services.llm import call_haiku_structured

logger = logging.getLogger(__name__)

# --- Domain reliability classification ----------------------------------------

_HIGH_RELIABILITY_PATTERNS: list[str] = [
    # Academic / journals
    r"\.edu$",
    r"arxiv\.org$",
    r"pubmed\.ncbi\.nlm\.nih\.gov$",
    r"scholar\.google\.com$",
    r"doi\.org$",
    r"nature\.com$",
    r"science\.org$",
    r"springer\.com$",
    r"wiley\.com$",
    r"ieee\.org$",
    r"acm\.org$",
    r"jstor\.org$",
    r"plos\.org$",
    # Government
    r"\.gov$",
    r"\.gov\.\w{2}$",
    # Standards bodies
    r"w3\.org$",
    r"ietf\.org$",
    r"iso\.org$",
]

_MEDIUM_RELIABILITY_PATTERNS: list[str] = [
    # Major news
    r"nytimes\.com$",
    r"washingtonpost\.com$",
    r"bbc\.(com|co\.uk)$",
    r"reuters\.com$",
    r"apnews\.com$",
    r"theguardian\.com$",
    r"economist\.com$",
    r"ft\.com$",
    r"wsj\.com$",
    r"bloomberg\.com$",
    # Major tech / reference
    r"wikipedia\.org$",
    r"github\.com$",
    r"stackoverflow\.com$",
    r"docs\.\w+\.com$",
    r"developer\.\w+\.com$",
    # Major publishers
    r"medium\.com$",
    r"substack\.com$",
]

_LOW_RELIABILITY_PATTERNS: list[str] = [
    r"reddit\.com$",
    r"quora\.com$",
    r"facebook\.com$",
    r"twitter\.com$",
    r"x\.com$",
    r"tiktok\.com$",
    r"instagram\.com$",
    r"pinterest\.com$",
    r"tumblr\.com$",
]


def _classify_domain(url: str) -> DomainReliability:
    """Classify a URL's domain into a reliability tier."""
    try:
        hostname = urlparse(url).hostname
    except Exception:
        return DomainReliability.UNKNOWN
    if not hostname:
        return DomainReliability.UNKNOWN

    hostname = hostname.lower()

    for pattern in _HIGH_RELIABILITY_PATTERNS:
        if re.search(pattern, hostname):
            return DomainReliability.HIGH

    for pattern in _MEDIUM_RELIABILITY_PATTERNS:
        if re.search(pattern, hostname):
            return DomainReliability.MEDIUM

    for pattern in _LOW_RELIABILITY_PATTERNS:
        if re.search(pattern, hostname):
            return DomainReliability.LOW

    return DomainReliability.UNKNOWN


# --- URL canonicalization for dedup -------------------------------------------

def _canonical_url(url: str) -> str:
    """Normalize a URL to a canonical form for dedup comparison."""
    parsed = urlparse(url)

    # Strip scheme, www prefix, trailing slash, common tracking params
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path.rstrip("/")

    # Drop common tracking query params
    return f"{host}{path}"


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
            canon = _canonical_url(source.original_url)
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

    # --- Step 2: Domain reliability --------------------------------------------
    for source in sources:
        if source.is_duplicate_of:
            continue

        if source.original_url:
            source.domain_reliability = _classify_domain(source.original_url)
        elif source.source_type == SourceType.PDF:
            # PDFs are typically curated uploads; default to MEDIUM
            source.domain_reliability = DomainReliability.MEDIUM
        # VIDEO/AUDIO default stays UNKNOWN

    # --- Step 3: Extract published_at with Haiku (for undated web sources) -----
    undated = [
        s for s in sources
        if s.published_at is None
        and s.text_preview
        and not s.is_duplicate_of
        and s.source_type == SourceType.WEB
    ]

    for source in undated:
        try:
            source.published_at = await _extract_date(source)
        except Exception:
            logger.warning(
                "Date extraction failed for source %s", source.id, exc_info=True
            )

    await db.flush()
    logger.info("Canonicalized %d sources for project %s", len(sources), project_id)


async def _extract_date(source: Source) -> datetime | None:
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
        data = json.loads(raw)
        date_str = data.get("date")
        if date_str:
            return datetime.strptime(date_str, "%Y-%m-%d")
    except (json.JSONDecodeError, ValueError):
        logger.debug("Could not parse date response: %s", raw)

    return None
