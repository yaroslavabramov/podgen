from __future__ import annotations

import hashlib
import json
from typing import Any
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

SOURCE_ACQUISITION_WRITE_MAX_CHARS = 9_000
SOURCE_ACQUISITION_TEXT_LIMITS: dict[str, dict[str, int]] = {
    "ingest_source_candidate": {
        "url": 2_048,
        "expected_editorial_function": 240,
        "rationale": 4_000,
        "source_type_hint": 64,
        "authority_hint": 120,
    },
    "reject_source_candidate": {"url": 2_048, "reason": 4_000},
    "finalize_source_acquisition_round": {"summary": 7_000},
}
SOURCE_ACQUISITION_LIST_LIMITS: dict[str, dict[str, tuple[int, int]]] = {
    "ingest_source_candidate": {"gap_ids": (100, 128)},
    "reject_source_candidate": {"gap_ids": (100, 128)},
    "finalize_source_acquisition_round": {"unresolved_gap_ids": (100, 128)},
}


def validate_source_acquisition_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    for field, max_length in SOURCE_ACQUISITION_TEXT_LIMITS.get(tool_name, {}).items():
        value = str(args.get(field) or "")
        if len(value) > max_length:
            return _semantic_too_long(field, max_length, len(value))
    for field, (max_items, max_item_length) in SOURCE_ACQUISITION_LIST_LIMITS.get(tool_name, {}).items():
        values = args.get(field) or []
        if not isinstance(values, list):
            return {"accepted": False, "error": f"semantic_field_invalid: field={field} expected=array"}
        if len(values) > max_items:
            return {
                "accepted": False,
                "error": (
                    f"semantic_collection_too_large: field={field} "
                    f"max_items={max_items} actual_items={len(values)}"
                ),
            }
        for index, item in enumerate(values):
            item_length = len(str(item or ""))
            if item_length > max_item_length:
                return _semantic_too_long(f"{field}[{index}]", max_item_length, item_length)
    payload_length = len(json.dumps(args, ensure_ascii=False, default=str))
    if payload_length > SOURCE_ACQUISITION_WRITE_MAX_CHARS:
        return {
            "accepted": False,
            "error": (
                f"semantic_payload_too_long: tool={tool_name} "
                f"max_length={SOURCE_ACQUISITION_WRITE_MAX_CHARS} actual_length={payload_length}"
            ),
        }
    return None


def _semantic_too_long(field: str, max_length: int, actual_length: int) -> dict[str, Any]:
    return {
        "accepted": False,
        "error": (
            f"semantic_field_too_long: field={field} "
            f"max_length={max_length} actual_length={actual_length}"
        ),
    }


def validate_candidate_static(candidate: SourceAcquisitionCandidate, gap_report: CorpusGapReport) -> str | None:
    if not candidate.normalized_url:
        return "Invalid or empty URL."
    if len(candidate.normalized_url) > 2048:
        return (
            "semantic_field_too_long: field=source.original_url "
            f"max_length=2048 actual_length={len(candidate.normalized_url)}"
        )
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
    title = path.replace("-", " ").replace("_", " ")
    if len(title) > 500:
        raise ValueError(
            f"semantic_field_too_long: field=source.title max_length=500 actual_length={len(title)}"
        )
    return title


def is_primaryish(candidate: SourceAcquisitionCandidate) -> bool:
    hint = f"{candidate.authority_hint} {candidate.normalized_url}".lower()
    return any(marker in hint for marker in ("official", "primary", ".gov", ".edu", "who.int", "sec.gov", "court", "archive"))


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
