"""Helpers for source canonicalization, reliability, and authority classification."""

import json
import logging
import re
import uuid
from urllib.parse import urlparse

from app.models.source import AuthorityType, DomainReliability, Source, SourceType
from app.pipeline.llm_json import parse_llm_json
from app.services.llm import call_haiku_structured

logger = logging.getLogger(__name__)

_HIGH_RELIABILITY_PATTERNS: list[str] = [
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
    r"\.gov$",
    r"\.gov\.\w{2}$",
    r"w3\.org$",
    r"ietf\.org$",
    r"iso\.org$",
]

_MEDIUM_RELIABILITY_PATTERNS: list[str] = [
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
    r"wikipedia\.org$",
    r"github\.com$",
    r"stackoverflow\.com$",
    r"docs\.\w+\.com$",
    r"developer\.\w+\.com$",
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

_PRIMARY_AUTHORITY_PATTERNS: list[str] = [
    r"\.gov$",
    r"\.gov\.\w{2}$",
    r"w3\.org$",
    r"ietf\.org$",
    r"iso\.org$",
    r"docs\.\w+\.com$",
    r"developer\.\w+\.com$",
]

_SECONDARY_AUTHORITY_PATTERNS: list[str] = [
    r"\.edu$",
    r"doi\.org$",
    r"arxiv\.org$",
    r"pubmed\.ncbi\.nlm\.nih\.gov$",
    r"nature\.com$",
    r"science\.org$",
    r"springer\.com$",
    r"wiley\.com$",
    r"ieee\.org$",
    r"acm\.org$",
    r"jstor\.org$",
    r"plos\.org$",
    r"diva-portal\.org$",
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
    r"medium\.com$",
    r"substack\.com$",
]

_TERTIARY_AUTHORITY_PATTERNS: list[str] = [
    r"wikipedia\.org$",
]

_PRIMARY_HINTS = (
    "act",
    "ordinance",
    "regulation",
    "policy statement",
    "official statistics",
    "annual report",
    "statistical bulletin",
    "official release",
)
_SECONDARY_HINTS = (
    "analysis",
    "explainer",
    "opinion",
    "column",
    "how ",
    "why ",
    "what it means",
    "guide",
    "research paper",
    "working paper",
    "journal article",
    "peer-reviewed",
)
_TERTIARY_HINTS = (
    "wikipedia",
    "encyclopedia",
    "overview",
    "summary",
)

_AUTHORITY_SYSTEM = """Classify the document authority level for fact verification.
Return JSON:
{
  "authority_type": "primary|secondary|tertiary|unknown"
}

Definitions:
- primary: original law, regulator/government statement, official data owner, official source-owner record, technical spec, or official documentation
- secondary: news, academic paper, working paper, analysis, explainer, commentary, article about the topic
- tertiary: encyclopedia, aggregator, reference compilation, summary of summaries
- unknown: cannot tell reliably from metadata/preview

Use only the provided URL, title, preview, and metadata. Do not use outside knowledge.
If the provided fields are insufficient, return unknown.
Be conservative. Output JSON only."""


def _hostname(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _matches_any(hostname: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, hostname) for pattern in patterns)


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def classify_domain(url: str) -> DomainReliability:
    hostname = _hostname(url)
    if not hostname:
        return DomainReliability.UNKNOWN
    if _matches_any(hostname, _HIGH_RELIABILITY_PATTERNS):
        return DomainReliability.HIGH
    if _matches_any(hostname, _MEDIUM_RELIABILITY_PATTERNS):
        return DomainReliability.MEDIUM
    if _matches_any(hostname, _LOW_RELIABILITY_PATTERNS):
        return DomainReliability.LOW
    return DomainReliability.UNKNOWN


def canonical_source_id(source: Source) -> uuid.UUID:
    return source.is_duplicate_of or source.id


def classify_authority_heuristic(
    *,
    source_type: str,
    original_url: str | None,
    title: str | None,
    text_preview: str | None,
) -> AuthorityType:
    hostname = _hostname(original_url)
    combined = " ".join(part for part in (title or "", text_preview or "") if part).lower()

    if hostname and _matches_any(hostname, _PRIMARY_AUTHORITY_PATTERNS):
        return AuthorityType.PRIMARY
    if hostname and _matches_any(hostname, _TERTIARY_AUTHORITY_PATTERNS):
        return AuthorityType.TERTIARY
    if hostname and _matches_any(hostname, _SECONDARY_AUTHORITY_PATTERNS):
        return AuthorityType.SECONDARY

    if source_type == SourceType.PDF:
        if any(hint in combined for hint in _PRIMARY_HINTS):
            return AuthorityType.PRIMARY
    if any(hint in combined for hint in _TERTIARY_HINTS):
        return AuthorityType.TERTIARY
    if any(hint in combined for hint in _SECONDARY_HINTS):
        return AuthorityType.SECONDARY
    if any(hint in combined for hint in _PRIMARY_HINTS):
        return AuthorityType.PRIMARY

    return AuthorityType.UNKNOWN


def audited_authority_type(source: Source) -> str:
    """Conservative authority label for generation-time source ranking.

    Older ingests may have broad primary labels from academic-domain or
    "research paper" heuristics. Generation should not treat those as primary
    evidence about external events unless the source owner is official/original.
    """
    authority = str(source.authority_type or AuthorityType.UNKNOWN)
    audited = classify_authority_heuristic(
        source_type=source.source_type,
        original_url=source.original_url,
        title=source.title,
        text_preview=source.text_preview,
    )
    if audited != AuthorityType.UNKNOWN:
        return str(audited)
    return authority


async def classify_source_authority(source: Source) -> AuthorityType:
    heuristic = classify_authority_heuristic(
        source_type=source.source_type,
        original_url=source.original_url,
        title=source.title,
        text_preview=source.text_preview,
    )
    if heuristic != AuthorityType.UNKNOWN:
        return heuristic

    preview = (source.text_preview or "").strip()
    title = (source.title or "").strip()
    if not title and not preview:
        return AuthorityType.UNKNOWN

    try:
        raw = await call_haiku_structured(
            system=_AUTHORITY_SYSTEM,
            prompt=json.dumps(
                {
                    "source_type": source.source_type,
                    "url": source.original_url,
                    "title": source.title,
                    "text_preview": preview[:3000],
                },
                ensure_ascii=False,
            ),
            max_tokens=256,
            temperature=0,
        )
        parsed = parse_llm_json(raw)
        if isinstance(parsed, dict):
            authority_raw = parsed.get("authority_type")
            if isinstance(authority_raw, str):
                try:
                    return AuthorityType(authority_raw.strip().lower())
                except ValueError:
                    pass
    except Exception:
        logger.warning("Authority classification failed for source %s", source.id, exc_info=True)

    return AuthorityType.UNKNOWN
