from __future__ import annotations

from typing import Any

from app.generation_v2.tools.evidence_window_refs import is_window_ref


def register_model_window_ref(owner: Any, *, source_ref: str, canonical_ref: str) -> str:
    """Replace a long signed ref with a short, session-scoped model handle."""

    by_canonical = getattr(owner, "_model_window_ref_by_canonical", None)
    by_handle = getattr(owner, "_canonical_window_ref_by_model_handle", None)
    counters = getattr(owner, "_model_window_ref_counters", None)
    if not isinstance(by_canonical, dict):
        by_canonical = {}
        setattr(owner, "_model_window_ref_by_canonical", by_canonical)
    if not isinstance(by_handle, dict):
        by_handle = {}
        setattr(owner, "_canonical_window_ref_by_model_handle", by_handle)
    if not isinstance(counters, dict):
        counters = {}
        setattr(owner, "_model_window_ref_counters", counters)

    existing = by_canonical.get(canonical_ref)
    if existing:
        return str(existing)
    normalized_source_ref = str(source_ref or "S??").strip() or "S??"
    next_index = int(counters.get(normalized_source_ref) or 0) + 1
    counters[normalized_source_ref] = next_index
    handle = f"W:{normalized_source_ref}:{next_index}"
    by_canonical[canonical_ref] = handle
    by_handle[handle] = canonical_ref
    return handle


def resolve_model_window_ref(owner: Any, value: str) -> str | None:
    text = str(value or "").strip()
    if is_window_ref(text):
        return text
    mapping = getattr(owner, "_canonical_window_ref_by_model_handle", None)
    if not isinstance(mapping, dict):
        return None
    resolved = mapping.get(text)
    return str(resolved) if resolved else None


__all__ = ["register_model_window_ref", "resolve_model_window_ref"]
