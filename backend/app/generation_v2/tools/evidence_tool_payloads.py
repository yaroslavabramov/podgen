from __future__ import annotations

from typing import Any

from app.models.generation_v2 import EvidenceItem, EvidenceLocatorType
from app.generation_v2.tools.evidence_store import compact_model_read_result


def compact_source_card(card: Any, *, canonical_kind: str | None = None, source_ref: str) -> dict[str, Any]:
    canonical_kind = canonical_kind or first_representation_kind(card)
    return {
        "source_ref": source_ref,
        "title": str(card.title or "")[:160].rstrip(),
        "source_type": card.source_type,
        "domain": card.domain,
        "published_at": card.published_at,
        "coverage_status": card.coverage_status,
        "canonical_kind": canonical_kind,
        "version_count": len(card.source_version_ids or []),
        "brief_hint": brief_hint(card),
    }


def active_filter_list(run_view: Any, key: str) -> list[str]:
    active_filter = getattr(run_view, "active_source_filter", None) or {}
    values = active_filter.get(key) or []
    return [str(item) for item in values if item]


def first_representation_kind(card: Any) -> str:
    summary = str(getattr(card, "representation_summary", "") or "")
    first = summary.split(",", 1)[0].strip()
    if ":" in first:
        return first.split(":", 1)[0].strip()
    return first or "unknown"


def brief_hint(card: Any) -> str:
    text = " ".join(str(getattr(card, "summary", "") or "").split())
    return text[:120].rstrip()


def cached_model_read_payload(evidence: EvidenceItem) -> dict[str, Any]:
    metadata = evidence.metadata_ or {}
    payload = model_read_agent_payload(
        {
            "supports_query": metadata.get("supports_query"),
            "support_level": metadata.get("support_level"),
            "locator_text": evidence.locator_text,
            "locator_type": evidence.locator_type,
            "confidence": evidence.score,
        }
    )
    payload["cache_hit"] = True
    return payload


def model_read_agent_payload(read_result: dict[str, Any]) -> dict[str, Any]:
    compact = compact_model_read_result(read_result)
    if "support_level" in compact:
        compact["support_assessment"] = compact.pop("support_level")
    if "supports_query" in compact:
        compact["supports_query"] = compact["supports_query"]
    return compact


def coverage_warning(item: EvidenceItem) -> str:
    metadata = item.metadata_ or {}
    if metadata.get("model_file_read_local_fallback"):
        return "Direct uploaded-file read was unavailable; this is a local fallback window and should be treated cautiously unless confirmed elsewhere."
    if metadata.get("source_version_fallback") or item.locator_type == EvidenceLocatorType.SOURCE_LEVEL:
        return "This is a broad source-level snapshot, not a precise cited passage; read a concrete source window before using it for an important claim."
    if metadata.get("fallback"):
        return "This came from local text/chunk fallback; use it as a lead and confirm important claims with a focused read."
    return ""
