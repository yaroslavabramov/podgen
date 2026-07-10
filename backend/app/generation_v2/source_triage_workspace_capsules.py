from __future__ import annotations

from typing import Any

from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_CAPSULE_PREVIEW_CHARS
from app.generation_v2.source_triage_workspace_state import now as _now
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.models.generation_v2 import SourceVersion


CAPSULE_SCHEMA_VERSION = "source_triage_capsule.v1"


async def build_capsule_for_card(evidence_toolset: Any, *, card: Any, source_ref: str) -> dict[str, Any]:
    version = await evidence_toolset._resolve_source_version(
        source_id=str(card.source_id),
        source_version_id="",
        source_ref="",
        version_ref="",
    )
    warnings = [str(item) for item in getattr(card, "gaps_or_cautions", []) or [] if str(item).strip()]
    body: dict[str, Any] | None = None
    if version is not None:
        cache = (version.metadata_ or {}).get("v2_triage_capsule") or {}
        cache_key = capsule_cache_key(version)
        if cache.get("schema_version") == CAPSULE_SCHEMA_VERSION and cache.get("cache_key") == cache_key:
            body = dict(cache.get("body") or {})
        if body is None:
            windows = await evidence_toolset._source_windows(version, max_windows=3)
            body = {
                "schema_version": CAPSULE_SCHEMA_VERSION,
                "cache_key": cache_key,
                "outline": [str(item.get("locator_text") or "")[:100] for item in windows if item.get("locator_text")][:4],
                "windows": [
                    {
                        "locator": str(item.get("locator_text") or "")[:100],
                        "locator_type": str(item.get("locator_type") or "unknown"),
                        "preview_text": " ".join(str(item.get("snippet_text") or "").split())[:TRIAGE_CAPSULE_PREVIEW_CHARS],
                    }
                    for item in windows[:2]
                    if str(item.get("snippet_text") or "").strip()
                ],
                "readability_warnings": warnings[:4],
                "readable": bool(windows),
            }
            version.metadata_ = {
                **(version.metadata_ or {}),
                "v2_triage_capsule": {
                    "schema_version": CAPSULE_SCHEMA_VERSION,
                    "cache_key": cache_key,
                    "body": body,
                    "updated_at": _now(),
                },
            }
            await evidence_toolset.db.flush()
    if body is None:
        body = {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "cache_key": "",
            "outline": [],
            "windows": [],
            "readability_warnings": [*warnings, "No readable local windows were available for orientation."],
            "readable": False,
        }
    return {
        "source_ref": source_ref,
        "title": str(getattr(card, "title", "") or "")[:180],
        "domain": getattr(card, "domain", None),
        "source_type": getattr(card, "source_type", None),
        "published_at": getattr(card, "published_at", None),
        "coverage_status": getattr(card, "coverage_status", None),
        "summary_hint": " ".join(str(getattr(card, "summary", "") or "").split())[:180],
        "outline": body.get("outline") or [],
        "windows": body.get("windows") or [],
        "readability_warnings": body.get("readability_warnings") or [],
        "readable": bool(body.get("readable")),
        "capsule_cache_hit": bool(
            (version is not None)
            and body.get("cache_key") == ((version.metadata_ or {}).get("v2_triage_capsule") or {}).get("cache_key")
        ),
    }


def metadata_only_capsule(inventory: SourceMap, source_ref: str) -> dict[str, Any]:
    context = build_source_ref_context(inventory)
    for card in inventory.source_cards:
        if source_ref_for_id(card.source_id, context) == source_ref:
            return {
                "source_ref": source_ref,
                "title": card.title[:180],
                "domain": card.domain,
                "source_type": card.source_type,
                "published_at": card.published_at,
                "summary_hint": " ".join(card.summary.split())[:180],
                "outline": [],
                "windows": [],
                "readability_warnings": ["Capsule has not been built yet."],
                "readable": False,
            }
    return {"source_ref": source_ref, "readable": False, "windows": [], "outline": []}


def source_order(inventory: SourceMap) -> list[str]:
    context = build_source_ref_context(inventory)
    return [source_ref_for_id(card.source_id, context) for card in inventory.source_cards]


def capsule_page_payload(*, source_count: int, capsules: list[dict[str, Any]], next_cursor: str) -> dict[str, Any]:
    return {
        "accepted": True,
        "source_count": source_count,
        "capsules": capsules,
        "next_cursor": next_cursor,
    }


def capsule_cache_key(version: SourceVersion) -> str:
    return f"{version.id}:{version.content_hash or ''}:{version.updated_at.isoformat() if version.updated_at else ''}:{CAPSULE_SCHEMA_VERSION}"


__all__ = [
    "CAPSULE_SCHEMA_VERSION",
    "build_capsule_for_card",
    "capsule_cache_key",
    "capsule_page_payload",
    "metadata_only_capsule",
    "source_order",
]
