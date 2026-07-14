from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app.generation_v2.source_corpus_provider_attributes import (
    parse_uuid,
    provider_attributes,
    provider_db_attributes,
    provider_external_attributes,
    provider_file_search_ready,
    provider_filename,
    provider_last_error,
    validate_reconciliation_candidate,
    version_uploadable,
)
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source

CANONICAL_RETRIEVAL_PRIORITY = {
    SourceRepresentationKind.ORIGINAL_FILE: 10,
    SourceRepresentationKind.TRANSCRIPT: 20,
    SourceRepresentationKind.EXTRACTED_TEXT: 30,
    SourceRepresentationKind.RENDERED_SNAPSHOT: 40,
}

def is_pdf_response(url: str, content_type: str, content: bytes) -> bool:
    normalized_type = (content_type or "").lower()
    path = urlparse(url).path.lower()
    if "application/pdf" in normalized_type:
        return True
    if content[:1024].lstrip().startswith(b"%PDF"):
        return True
    if path.endswith(".pdf") and (not normalized_type or "octet-stream" in normalized_type):
        return True
    return False


def filename_from_url(url: str, *, default: str, required_suffix: str | None = None) -> str:
    path_name = Path(unquote(urlparse(url).path)).name
    filename = path_name or default
    if "." not in filename:
        filename = default
    if required_suffix and Path(filename).suffix.lower() != required_suffix.lower():
        normalized_suffix = required_suffix if required_suffix.startswith(".") else f".{required_suffix}"
        if Path(filename).suffix.lower() != normalized_suffix.lower():
            filename = f"{filename}{normalized_suffix}"
    return filename.replace("/", "_")


def agent_facing_provider_versions(source_versions: list[SourceVersion]) -> list[SourceVersion]:
    """Return the single canonical provider-search version per source."""
    ready_versions = [version for version in source_versions if version_uploadable(version)]
    agent_versions: list[SourceVersion] = []
    for versions in versions_by_source(ready_versions).values():
        canonical = canonical_provider_version(versions)
        if canonical is not None:
            agent_versions.append(canonical)
    return agent_versions


def canonical_provider_version(source_versions: list[SourceVersion]) -> SourceVersion | None:
    candidates: list[tuple[int, datetime, str, SourceVersion]] = []
    for version in source_versions:
        if not version_uploadable(version):
            continue
        try:
            kind = SourceRepresentationKind(str(version.kind))
        except ValueError:
            continue
        priority = CANONICAL_RETRIEVAL_PRIORITY.get(kind)
        if priority is None:
            continue
        candidates.append((priority, version.created_at or datetime.min, str(version.id), version))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def versions_by_source(source_versions: list[SourceVersion]) -> dict[uuid.UUID, list[SourceVersion]]:
    grouped: dict[uuid.UUID, list[SourceVersion]] = {}
    for version in source_versions:
        grouped.setdefault(version.source_id, []).append(version)
    return grouped


def version_kind_counts(source_versions) -> dict[str, int]:
    counts: dict[str, int] = {}
    for version in source_versions:
        key = str(version.kind)
        counts[key] = counts.get(key, 0) + 1
    return counts


def version_has_snapshot(version: SourceVersion) -> bool:
    return bool(
        version.storage_path
        and version.status == V2RecordStatus.READY
        and version.kind
        in {
            SourceRepresentationKind.FETCHED_HTML,
            SourceRepresentationKind.RENDERED_SNAPSHOT,
            SourceRepresentationKind.MODEL_READ_TEXT,
            SourceRepresentationKind.EXTRACTED_TEXT,
            SourceRepresentationKind.PAGE_MARKED_TEXT,
            SourceRepresentationKind.TRANSCRIPT,
        }
    )


def page_mark_text(text: str, source: Source) -> str:
    paragraphs = [paragraph.strip() for paragraph in text.split("\n") if paragraph.strip()]
    lines = [f"[source_id={source.id} page_marker=1 paragraph={idx + 1}] {paragraph}" for idx, paragraph in enumerate(paragraphs)]
    return "\n\n".join(lines)


def evidence_to_dict(item) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "generation_run_id": str(item.generation_run_id),
        "corpus_revision": int(getattr(item, "corpus_revision", 1) or 1),
        "attempt": int(getattr(item, "attempt", 1) or 1),
        "project_id": str(item.project_id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "provider": item.provider,
        "provider_vector_store_id": item.provider_vector_store_id,
        "provider_file_id": item.provider_file_id,
        "provider_vector_store_file_id": item.provider_vector_store_file_id,
        "response_id": item.response_id,
        "tool_call_id": item.tool_call_id,
        "retrieval_channel": item.retrieval_channel,
        "query_text": item.query_text,
        "purpose": item.purpose,
        "search_result_index": item.search_result_index,
        "score": item.score,
        "snippet_text": item.snippet_text,
        "normalized_quote_text": item.normalized_quote_text,
        "locator_text": item.locator_text,
        "locator_type": item.locator_type,
        "evidence_strength": item.evidence_strength,
        "snippet_hash": item.snippet_hash,
        "metadata": item.metadata_,
        "created_at": item.created_at.isoformat() if isinstance(item.created_at, datetime) else None,
    }


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def versioned_object_name(source_id: uuid.UUID, content_hash: str, filename: str) -> str:
    return f"v2/source_versions/{source_id}/{content_hash}/{filename}"

__all__ = [
    "parse_uuid",
    "provider_attributes",
    "provider_db_attributes",
    "provider_external_attributes",
    "provider_file_search_ready",
    "provider_filename",
    "provider_last_error",
    "validate_reconciliation_candidate",
]
