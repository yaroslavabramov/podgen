from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def search_cache_key(run_view: Any, *, query: str, source_ids: list[str], limit: int) -> str:
    payload = {
        "type": "search_corpus",
        "query": cache_text(query),
        "source_ids": sorted(str(item) for item in (source_ids or getattr(run_view, "selected_source_ids", []) or [])),
        "limit": int(limit),
        "corpus_hash": run_corpus_snapshot_hash(run_view),
    }
    return cache_key(payload)


def model_read_cache_key(
    *,
    source_version_id: str,
    focus_question: str,
    claim_text: str,
    purpose: str,
    locator_hint: str,
    read_surface_id: str,
) -> str:
    payload = {
        "type": "read_source_with_model",
        "source_version_id": str(source_version_id),
        "focus_question": cache_text(focus_question),
        "claim_text": cache_text(claim_text),
        "purpose": cache_text(purpose),
        "locator_hint": cache_text(locator_hint),
        "read_surface_id": str(read_surface_id or ""),
    }
    return cache_key(payload)


def model_read_request_cache_key(
    *,
    run_view: Any,
    source_id: str,
    source_version_id: str,
    focus_question: str,
    claim_text: str,
    purpose: str,
    locator_hint: str,
) -> str:
    payload = {
        "type": "read_source_with_model_request",
        "source_id": str(source_id or ""),
        "source_version_id": str(source_version_id or ""),
        "focus_question": cache_text(focus_question),
        "claim_text": cache_text(claim_text),
        "purpose": cache_text(purpose),
        "locator_hint": cache_text(locator_hint),
        "corpus_hash": run_corpus_snapshot_hash(run_view),
    }
    return cache_key(payload)


def run_corpus_snapshot_hash(run_view: Any) -> str:
    manifest_hash = str(getattr(run_view, "manifest_hash", "") or "")
    if manifest_hash:
        return manifest_hash
    payload = {
        "source_ids": sorted(str(item) for item in (getattr(run_view, "selected_source_ids", []) or [])),
        "source_version_ids": sorted(str(item) for item in (getattr(run_view, "selected_source_version_ids", []) or [])),
    }
    return cache_key(payload)


def cache_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def cache_key(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
