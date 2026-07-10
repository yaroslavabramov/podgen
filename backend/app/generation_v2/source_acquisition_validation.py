from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import settings
from app.generation_v2.schemas import CorpusGapReport, SourceAcquisitionCandidate

UNSUPPORTED_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".dmg",
    ".iso",
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
}
SEARCH_HOSTS = {
    "google.com",
    "www.google.com",
    "bing.com",
    "www.bing.com",
    "duckduckgo.com",
    "search.yahoo.com",
}
SOCIAL_HOSTS = {
    "x.com",
    "twitter.com",
    "www.twitter.com",
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "linkedin.com",
    "www.linkedin.com",
}


def validate_candidate_static(candidate: SourceAcquisitionCandidate, gap_report: CorpusGapReport) -> str | None:
    if not candidate.normalized_url:
        return "Invalid or empty URL."
    parsed = urlparse(candidate.normalized_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "Candidate URL must be http(s)."
    host = (parsed.hostname or "").lower()
    if host in SEARCH_HOSTS or (host.endswith("google.com") and parsed.path.startswith("/search")):
        return "Search result pages are not source material."
    if host in SOCIAL_HOSTS:
        return "Social posts are not accepted as source-acquisition corpus material by default."
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in UNSUPPORTED_EXTENSIONS):
        return "Unsupported file type for V2 source acquisition."
    allowed_gap_ids = {gap.gap_id for gap in gap_report.gaps}
    unknown_gap_ids = [gap_id for gap_id in candidate.gap_ids if gap_id not in allowed_gap_ids]
    if not candidate.gap_ids or unknown_gap_ids:
        return "Candidate must map to known gap_ids from the current CorpusGapReport."
    if candidate.confidence < float(settings.generation_v2_source_acquisition_min_confidence):
        return "Candidate confidence is below the acquisition threshold."
    if not candidate.rationale.strip():
        return "Candidate rationale is required."
    return None


def normalize_candidate_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    query = urlencode(query_items, doseq=True)
    return urlunparse((scheme, netloc, path.rstrip("/") or "/", "", query, ""))


def title_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").split("/")[-1] or parsed.hostname or "Acquired source"
    return path.replace("-", " ").replace("_", " ")[:500]


def is_primaryish(candidate: SourceAcquisitionCandidate) -> bool:
    hint = f"{candidate.authority_hint} {candidate.normalized_url}".lower()
    return any(marker in hint for marker in ("official", "primary", ".gov", ".edu", "who.int", "sec.gov", "court", "archive"))


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
