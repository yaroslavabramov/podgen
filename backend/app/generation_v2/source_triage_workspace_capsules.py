from __future__ import annotations

import uuid
from typing import Any

from app.generation_v2.model_context_text import (
    is_generic_window_locator,
    model_preview_text,
    stable_unique_texts,
)
from app.generation_v2.schemas import SourceMap, SourceProfile
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_CAPSULE_PREVIEW_CHARS
from app.generation_v2.source_triage_workspace_state import now as _now
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.models.generation_v2 import ProjectSourceProfile, SourceVersion


CAPSULE_SCHEMA_VERSION = "source_triage_capsule.v4"
_CACHE_SOURCE_REF_MARKER = "\ue000"
_TITLE_PREVIEW_CHARS = 180
_DOMAIN_PREVIEW_CHARS = 180
_SUMMARY_PREVIEW_CHARS = 180
_OUTLINE_PREVIEW_CHARS = 180
_OUTLINE_PREVIEW_ITEMS = 8
_WARNING_PREVIEW_CHARS = 300
_WARNING_PREVIEW_ITEMS = 8
_LOCATOR_PREVIEW_CHARS = 100


async def build_capsule_for_card(evidence_toolset: Any, *, card: Any, source_ref: str) -> dict[str, Any]:
    profile = None
    profile_id = str(getattr(card, "source_profile_id", None) or "")
    if profile_id:
        try:
            profile_record = await evidence_toolset.db.get(ProjectSourceProfile, uuid.UUID(profile_id))
        except (TypeError, ValueError):
            profile_record = None
        if (
            profile_record is not None
            and str(profile_record.source_id) == str(card.source_id)
            and str(profile_record.source_version_id)
            in {str(item) for item in (getattr(card, "source_version_ids", []) or [])}
            and bool(profile_record.is_current)
        ):
            profile = SourceProfile.model_validate(profile_record.payload)
    version = await evidence_toolset._resolve_source_version(
        source_id=str(card.source_id),
        source_version_id="",
        source_ref="",
        version_ref="",
    )
    replacements = _source_replacements(card, source_ref)
    body: dict[str, Any] | None = None
    if version is not None:
        cache = (version.metadata_ or {}).get("v2_triage_capsule") or {}
        cache_key = capsule_cache_key(version)
        if cache.get("schema_version") == CAPSULE_SCHEMA_VERSION and cache.get("cache_key") == cache_key:
            body = dict(cache.get("body") or {})
        if body is None:
            windows = await evidence_toolset._source_windows(version, max_windows=3)
            capsule_windows = [
                _capsule_window(item, internal_ids=replacements)
                for item in windows
                if isinstance(item, dict) and str(item.get("snippet_text") or "").strip()
            ][:2]
            body = {
                # This cache is source-version scoped, while source refs are run scoped.
                # Keep only ref-neutral window previews here and replace known ids below.
                "windows": capsule_windows,
                "text_available": bool(capsule_windows),
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
            "windows": [],
            "text_available": False,
        }
    outline = _sanitized_unique_texts(
        (
            item
            for item in (profile.outline_hints if profile is not None else [])
            if not is_generic_window_locator(item)
        ),
        replacements=replacements,
    )
    readability_warnings = _sanitized_unique_texts(
        [
            *(getattr(card, "gaps_or_cautions", []) or []),
            *(profile.readability_warnings if profile is not None else []),
        ],
        replacements=replacements,
    )
    windows = [
        {
            "locator": model_preview_text(
                item.get("locator") or "",
                replacements={**replacements, _CACHE_SOURCE_REF_MARKER: source_ref},
            )[
                :_LOCATOR_PREVIEW_CHARS
            ],
            "locator_chars": _sanitized_original_chars(
                item.get("locator") or "",
                stored_chars=item.get("locator_chars"),
                stored_ref_count=item.get("locator_source_ref_count"),
                source_ref=source_ref,
                replacements=replacements,
            ),
            "locator_truncated": _stored_text_was_truncated(
                item.get("locator") or "",
                stored_chars=item.get("locator_chars"),
                stored_ref_count=item.get("locator_source_ref_count"),
                stored_truncated=item.get("locator_truncated"),
                limit=_LOCATOR_PREVIEW_CHARS,
                source_ref=source_ref,
                replacements=replacements,
            ),
            "locator_type": str(item.get("locator_type") or "unknown"),
            "preview_text": model_preview_text(
                item.get("preview_text") or "",
                replacements={**replacements, _CACHE_SOURCE_REF_MARKER: source_ref},
            )[:TRIAGE_CAPSULE_PREVIEW_CHARS],
            "preview_text_chars": _sanitized_original_chars(
                item.get("preview_text") or "",
                stored_chars=item.get("preview_text_chars"),
                stored_ref_count=item.get("preview_text_source_ref_count"),
                source_ref=source_ref,
                replacements=replacements,
            ),
            "preview_text_truncated": _stored_text_was_truncated(
                item.get("preview_text") or "",
                stored_chars=item.get("preview_text_chars"),
                stored_ref_count=item.get("preview_text_source_ref_count"),
                stored_truncated=item.get("preview_text_truncated"),
                limit=TRIAGE_CAPSULE_PREVIEW_CHARS,
                source_ref=source_ref,
                replacements=replacements,
            ),
        }
        for item in body.get("windows") or []
        if isinstance(item, dict)
        and model_preview_text(
            item.get("preview_text") or "",
            replacements={**replacements, _CACHE_SOURCE_REF_MARKER: source_ref},
        )
    ]
    title = model_preview_text(getattr(card, "title", "") or "", replacements=replacements)
    domain = model_preview_text(getattr(card, "domain", "") or "", replacements=replacements)
    orientation = model_preview_text(getattr(card, "summary", "") or "", replacements=replacements)
    return {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "source_ref": source_ref,
        "title": title[:_TITLE_PREVIEW_CHARS],
        "title_chars": len(title),
        "title_truncated": len(title) > _TITLE_PREVIEW_CHARS,
        "domain": domain[:_DOMAIN_PREVIEW_CHARS],
        "domain_chars": len(domain),
        "domain_truncated": len(domain) > _DOMAIN_PREVIEW_CHARS,
        "source_type": getattr(card, "source_type", None),
        "published_at": getattr(card, "published_at", None),
        "coverage_status": getattr(card, "coverage_status", None),
        "summary_hint": orientation[:_SUMMARY_PREVIEW_CHARS],
        "summary_hint_chars": len(orientation),
        "summary_hint_truncated": len(orientation) > _SUMMARY_PREVIEW_CHARS,
        "outline": [item[:_OUTLINE_PREVIEW_CHARS] for item in outline[:_OUTLINE_PREVIEW_ITEMS]],
        "outline_item_chars": [len(item) for item in outline[:_OUTLINE_PREVIEW_ITEMS]],
        "outline_total_count": len(outline),
        "outline_omitted_count": max(0, len(outline) - _OUTLINE_PREVIEW_ITEMS),
        "windows": windows,
        "readability_warnings": [
            item[:_WARNING_PREVIEW_CHARS] for item in readability_warnings[:_WARNING_PREVIEW_ITEMS]
        ],
        "readability_warning_item_chars": [
            len(item) for item in readability_warnings[:_WARNING_PREVIEW_ITEMS]
        ],
        "readability_warning_total_count": len(readability_warnings),
        "readability_warning_omitted_count": max(
            0,
            len(readability_warnings) - _WARNING_PREVIEW_ITEMS,
        ),
        "text_available": bool(body.get("text_available", body.get("readable"))),
    }


def metadata_only_capsule(inventory: SourceMap, source_ref: str) -> dict[str, Any]:
    context = build_source_ref_context(inventory)
    for card in inventory.source_cards:
        if source_ref_for_id(card.source_id, context) == source_ref:
            replacements = _source_replacements(card, source_ref)
            title = model_preview_text(card.title, replacements=replacements)
            domain = model_preview_text(card.domain, replacements=replacements)
            orientation = model_preview_text(card.summary, replacements=replacements)
            return {
                "schema_version": CAPSULE_SCHEMA_VERSION,
                "source_ref": source_ref,
                "title": title[:_TITLE_PREVIEW_CHARS],
                "title_chars": len(title),
                "title_truncated": len(title) > _TITLE_PREVIEW_CHARS,
                "domain": domain[:_DOMAIN_PREVIEW_CHARS],
                "domain_chars": len(domain),
                "domain_truncated": len(domain) > _DOMAIN_PREVIEW_CHARS,
                "source_type": card.source_type,
                "published_at": card.published_at,
                "coverage_status": card.coverage_status,
                "summary_hint": orientation[:_SUMMARY_PREVIEW_CHARS],
                "summary_hint_chars": len(orientation),
                "summary_hint_truncated": len(orientation) > _SUMMARY_PREVIEW_CHARS,
                "outline": [],
                "outline_item_chars": [],
                "outline_total_count": 0,
                "outline_omitted_count": 0,
                "windows": [],
                "readability_warnings": [],
                "readability_warning_item_chars": [],
                "readability_warning_total_count": 0,
                "readability_warning_omitted_count": 0,
                "text_available": False,
            }
    return {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "source_ref": source_ref,
        "text_available": False,
        "windows": [],
        "outline": [],
        "outline_item_chars": [],
        "outline_total_count": 0,
        "outline_omitted_count": 0,
        "readability_warnings": [],
        "readability_warning_item_chars": [],
        "readability_warning_total_count": 0,
        "readability_warning_omitted_count": 0,
    }


def _capsule_window(item: dict[str, Any], *, internal_ids: Any) -> dict[str, Any]:
    cache_replacements = {
        str(internal_id): _CACHE_SOURCE_REF_MARKER for internal_id in internal_ids if str(internal_id)
    }
    preview = model_preview_text(item.get("snippet_text") or "", replacements=cache_replacements)
    locator = model_preview_text(item.get("locator_text") or "", replacements=cache_replacements)
    return {
        "locator": locator[:_LOCATOR_PREVIEW_CHARS],
        "locator_chars": len(locator),
        "locator_source_ref_count": locator.count(_CACHE_SOURCE_REF_MARKER),
        "locator_truncated": len(locator) > _LOCATOR_PREVIEW_CHARS,
        "locator_type": str(item.get("locator_type") or "unknown"),
        "preview_text": preview[:TRIAGE_CAPSULE_PREVIEW_CHARS],
        "preview_text_chars": len(preview),
        "preview_text_source_ref_count": preview.count(_CACHE_SOURCE_REF_MARKER),
        "preview_text_truncated": len(preview) > TRIAGE_CAPSULE_PREVIEW_CHARS,
    }


def _source_replacements(card: Any, source_ref: str) -> dict[str, str]:
    internal_ids = [
        str(getattr(card, "source_id", "") or ""),
        *(str(item) for item in (getattr(card, "source_version_ids", []) or [])),
        *(str(item) for item in (getattr(card, "representation_ids", []) or [])),
        str(getattr(card, "source_profile_id", "") or ""),
    ]
    return {internal_id: source_ref for internal_id in internal_ids if internal_id}


def _sanitized_unique_texts(values: Any, *, replacements: dict[str, str]) -> list[str]:
    return stable_unique_texts(
        model_preview_text(value, replacements=replacements)
        for value in values
    )


def _sanitized_original_chars(
    stored_preview: Any,
    *,
    stored_chars: Any,
    stored_ref_count: Any,
    source_ref: str,
    replacements: dict[str, str],
) -> int:
    preview = model_preview_text(
        stored_preview,
        replacements={**replacements, _CACHE_SOURCE_REF_MARKER: source_ref},
    )
    raw_preview = model_preview_text(stored_preview)
    original_chars = int(stored_chars) if stored_chars is not None else len(raw_preview)
    if stored_ref_count is not None:
        return max(
            len(preview),
            original_chars + int(stored_ref_count) * (len(source_ref) - len(_CACHE_SOURCE_REF_MARKER)),
        )
    # Known infrastructure ids can only make the model-facing text shorter.
    return max(len(preview), original_chars - max(0, len(raw_preview) - len(preview)))


def _stored_text_was_truncated(
    stored_preview: Any,
    *,
    stored_chars: Any,
    stored_ref_count: Any,
    stored_truncated: Any,
    limit: int,
    source_ref: str,
    replacements: dict[str, str],
) -> bool:
    return bool(stored_truncated) or _sanitized_original_chars(
        stored_preview,
        stored_chars=stored_chars,
        stored_ref_count=stored_ref_count,
        source_ref=source_ref,
        replacements=replacements,
    ) > limit


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
    return ":".join(
        [
            str(version.id),
            str(version.content_hash or ""),
            str(getattr(version, "kind", "") or ""),
            str(getattr(version, "storage_path", "") or ""),
            str(getattr(version, "provider_file_id", "") or ""),
            CAPSULE_SCHEMA_VERSION,
        ]
    )


__all__ = [
    "CAPSULE_SCHEMA_VERSION",
    "build_capsule_for_card",
    "capsule_cache_key",
    "capsule_page_payload",
    "metadata_only_capsule",
    "source_order",
]
