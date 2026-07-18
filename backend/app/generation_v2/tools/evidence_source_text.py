from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.generation_v2.source_content_readiness import extract_web_document_text
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion
from app.services.storage import download_text, get_minio_client


_WORD_RE = re.compile(r"[^\W_]{3,}", re.UNICODE)
_INTERNAL_PAGE_MARKER_RE = re.compile(
    r"\[source_id=[0-9a-f-]{36}\s+(page_marker=\d+\s+paragraph=\d+)\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceTextLoadResult:
    text: str
    status: str
    error_detail: str = ""


def model_read_text_surface(text: str) -> str:
    """Remove only storage-owned source IDs while preserving document markers."""

    return _INTERNAL_PAGE_MARKER_RE.sub(r"[\1]", text)


def load_source_version_text(version: SourceVersion) -> SourceTextLoadResult:
    if not version.storage_path or version.kind == SourceRepresentationKind.ORIGINAL_URL:
        return SourceTextLoadResult(text="", status="not_text_readable")
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE and not looks_text_readable_path(version.storage_path):
        return SourceTextLoadResult(text="", status="not_text_readable")
    try:
        stored_text = download_text(get_minio_client(), version.storage_path)
    except Exception as exc:
        return SourceTextLoadResult(
            text="",
            status="storage_unavailable",
            error_detail=type(exc).__name__,
        )
    expected_hash = str(version.content_hash or "").strip().lower()
    if expected_hash and hashlib.sha256(stored_text.encode("utf-8")).hexdigest() != expected_hash:
        return SourceTextLoadResult(text="", status="integrity_error")
    text = (
        extract_web_document_text(stored_text)
        if version.kind == SourceRepresentationKind.FETCHED_HTML
        else stored_text
    )
    text = model_read_text_surface(text)
    if not text:
        return SourceTextLoadResult(text="", status="empty")
    return SourceTextLoadResult(text=text, status="ready")


def source_version_text(version: SourceVersion) -> str:
    """Compatibility string API; triage uses the typed loader to surface failures."""

    return load_source_version_text(version).text


def best_readable_source_version(
    versions: Iterable[SourceVersion],
    *,
    text_getter: Callable[[SourceVersion], str] | None = None,
) -> SourceVersion | None:
    """Choose the preferred immutable read surface, recovering under-extraction.

    Kind priority remains the default.  A lower-priority representation only wins
    when it is a materially fuller view of the same document.  This keeps old run
    manifests readable: a fetched HTML version can supply a newly improved,
    deterministic read view without rewriting the pinned stored extraction.
    """
    from app.generation_v2.tools.evidence_inventory_helpers import readable_version_priority

    version_list = list(versions)
    direct_parent_ids = {
        str(metadata.get("derived_from_source_version_id"))
        for version in version_list
        for metadata in [version.metadata_ if isinstance(version.metadata_, dict) else {}]
        if metadata.get("derived_from_source_version_id")
    }
    ordered = sorted(
        version_list,
        key=lambda version: (
            readable_version_priority(version.kind),
            0 if str(version.id) in direct_parent_ids else 1,
            str(version.id),
        ),
    )
    if not ordered:
        return None
    getter = text_getter or source_version_text
    texts = {str(version.id): getter(version).strip() for version in ordered}
    best = ordered[0]
    for candidate in ordered[1:]:
        if _is_materially_fuller_view(
            best,
            texts.get(str(best.id), ""),
            candidate,
            texts.get(str(candidate.id), ""),
        ):
            best = candidate
    return best


def _is_materially_fuller_view(
    preferred: SourceVersion,
    preferred_text: str,
    candidate: SourceVersion,
    candidate_text: str,
) -> bool:
    if not candidate_text:
        return False
    if not preferred_text:
        return True
    added_chars = len(candidate_text) - len(preferred_text)
    if added_chars < max(1000, len(preferred_text) // 5):
        return False

    preferred_words = set(_WORD_RE.findall(preferred_text.casefold()))
    candidate_words = set(_WORD_RE.findall(candidate_text.casefold()))
    retained_ratio = (
        len(preferred_words.intersection(candidate_words)) / len(preferred_words)
        if preferred_words
        else 0.0
    )

    # A persisted extraction that names this immutable capture as its parent is
    # directly comparable to the new deterministic extraction of that capture,
    # but lineage alone does not prove that a longer result is not a navigation
    # shell. It must retain a clear majority of the prior document vocabulary.
    preferred_metadata = preferred.metadata_ if isinstance(preferred.metadata_, dict) else {}
    if str(preferred_metadata.get("derived_from_source_version_id") or "") == str(candidate.id):
        return retained_ratio >= 0.65

    # For independent representations, require the candidate to retain most of
    # the preferred vocabulary while adding substantial new vocabulary.  This
    # avoids choosing an unrelated long navigation/rendering shell by length.
    if not preferred_words:
        return len(candidate_text) >= len(preferred_text) * 2
    added_words = len(candidate_words.difference(preferred_words))
    return retained_ratio >= 0.8 and added_words >= max(80, len(preferred_words) // 4)


def looks_text_readable_path(path: str) -> bool:
    lowered = path.lower().split("?", 1)[0]
    return lowered.endswith((".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv", ".tsv", ".xml", ".srt", ".vtt"))


__all__ = [
    "SourceTextLoadResult",
    "best_readable_source_version",
    "load_source_version_text",
    "looks_text_readable_path",
    "model_read_text_surface",
    "source_version_text",
]
