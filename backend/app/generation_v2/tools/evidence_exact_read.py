from __future__ import annotations

import hashlib
from typing import Any

from app.generation_v2.corpus_scope import scope_for_run_view


EXACT_READ_PROVENANCE_KEY = "exact_read_provenance"
EXACT_READ_PROGRESS_KEY = "exact_read_progress"


def exact_read_provenance(owner: Any, evidence: Any, *, method: str) -> dict[str, Any]:
    scope = scope_for_run_view(getattr(owner, "run_view", None))
    return {
        "generation_run_id": str(getattr(getattr(owner, "run", None), "id", "") or ""),
        "corpus_revision": scope.revision,
        "attempt": scope.attempt,
        "evidence_id": str(getattr(evidence, "id", "") or ""),
        "source_version_id": str(getattr(evidence, "source_version_id", "") or ""),
        "snippet_fingerprint": _evidence_snippet_fingerprint(evidence),
        "method": str(method or "exact_read"),
    }


def evidence_has_current_exact_read(owner: Any, evidence: Any) -> bool:
    metadata = getattr(evidence, "metadata_", None) or {}
    provenance = metadata.get(EXACT_READ_PROVENANCE_KEY)
    if not isinstance(provenance, dict):
        return False
    scope = scope_for_run_view(getattr(owner, "run_view", None))
    return (
        str(provenance.get("generation_run_id") or "")
        == str(getattr(getattr(owner, "run", None), "id", "") or "")
        and int(provenance.get("corpus_revision") or 0) == scope.revision
        and int(provenance.get("attempt") or 0) == scope.attempt
        and str(provenance.get("evidence_id") or "") == str(getattr(evidence, "id", "") or "")
        and str(provenance.get("source_version_id") or "")
        == str(getattr(evidence, "source_version_id", "") or "")
        and str(provenance.get("snippet_fingerprint") or "") == _evidence_snippet_fingerprint(evidence)
    )


async def mark_evidence_exact_read(
    owner: Any,
    evidence: Any,
    *,
    method: str,
    read_range: tuple[int, int] | None = None,
    total_chars: int | None = None,
) -> bool:
    """Persist exact-read lineage; paged evidence becomes usable only after every part was read."""

    if not hasattr(evidence, "metadata_") or not getattr(evidence, "source_version_id", None):
        return False
    metadata = dict(getattr(evidence, "metadata_", None) or {})
    provenance = exact_read_provenance(owner, evidence, method=method)
    if read_range is None:
        metadata[EXACT_READ_PROVENANCE_KEY] = provenance
        metadata.pop(EXACT_READ_PROGRESS_KEY, None)
    else:
        start, end = read_range
        total = max(0, int(total_chars or 0))
        if start < 0 or end <= start or end > total:
            return False
        existing = metadata.get(EXACT_READ_PROGRESS_KEY)
        same_scope = isinstance(existing, dict) and all(
            existing.get(key) == provenance.get(key)
            for key in (
                "generation_run_id",
                "corpus_revision",
                "attempt",
                "evidence_id",
                "source_version_id",
                "snippet_fingerprint",
            )
        )
        ranges = list(existing.get("ranges") or []) if same_scope else []
        ranges.append([start, end])
        merged = _merge_ranges(ranges)
        metadata[EXACT_READ_PROGRESS_KEY] = {
            **provenance,
            "total_chars": total,
            "ranges": merged,
        }
        if merged == [[0, total]]:
            metadata[EXACT_READ_PROVENANCE_KEY] = provenance
            metadata.pop(EXACT_READ_PROGRESS_KEY, None)
    evidence.metadata_ = metadata
    flush = getattr(getattr(owner, "db", None), "flush", None)
    if callable(flush):
        await flush()
    return evidence_has_current_exact_read(owner, evidence)


def _merge_ranges(values: list[Any]) -> list[list[int]]:
    parsed: list[tuple[int, int]] = []
    for value in values:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        try:
            start, end = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            continue
        if start >= 0 and end > start:
            parsed.append((start, end))
    merged: list[list[int]] = []
    for start, end in sorted(parsed):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def _evidence_snippet_fingerprint(evidence: Any) -> str:
    stored_hash = str(getattr(evidence, "snippet_hash", "") or "").strip()
    if stored_hash:
        return stored_hash
    snippet = str(getattr(evidence, "snippet_text", "") or "")
    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()


__all__ = [
    "EXACT_READ_PROGRESS_KEY",
    "EXACT_READ_PROVENANCE_KEY",
    "evidence_has_current_exact_read",
    "exact_read_provenance",
    "mark_evidence_exact_read",
]
