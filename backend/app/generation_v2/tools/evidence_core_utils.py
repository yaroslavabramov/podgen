from __future__ import annotations

import re
import uuid
from typing import Any

from app.generation_v2.tools.evidence_locator_parsing import query_tokens as _query_tokens
from app.generation_v2.tools.evidence_source_text import looks_text_readable_path as _looks_text_readable_path
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceRetrievalChannel,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)


def unsupported_model_read_selected_as_support(item: EvidenceItem, *, purpose: str, role: str) -> bool:
    retrieval_channel = evidence_origin_retrieval_channel(item)
    if retrieval_channel != EvidenceRetrievalChannel.MODEL_FILE_READ:
        return False
    metadata = item.metadata_ or {}
    support_level = str(metadata.get("support_level") or "")
    if bool(metadata.get("supports_query", True)) and support_level not in {"weak", "unsupported", "not_found"}:
        return False
    allowed = {
        "gap",
        "gaps",
        "caution",
        "uncertainty",
        "contradiction",
        "negative_finding",
        "no_support",
        "not_found",
    }
    purpose_role = {part.lower().strip() for part in [purpose, role] if part}
    return not bool(purpose_role.intersection(allowed))


def evidence_origin_retrieval_channel(item: EvidenceItem) -> str:
    retrieval_channel = str(getattr(item, "retrieval_channel", "") or "")
    if retrieval_channel == EvidenceRetrievalChannel.ATTEMPT_LINEAGE:
        metadata = getattr(item, "metadata_", None) or {}
        return str(metadata.get("origin_retrieval_channel") or retrieval_channel)
    return retrieval_channel


def quote_text_matches_evidence(quote: str, snippet: str) -> bool:
    normalized_quote = normalize(quote)
    normalized_snippet = normalize(snippet)
    if not normalized_quote or not normalized_snippet:
        return False
    if normalized_quote in normalized_snippet:
        return True
    quote_tokens = set(_query_tokens(normalized_quote))
    if not quote_tokens:
        return False
    overlap = quote_tokens.intersection(_query_tokens(normalized_snippet))
    return len(overlap) / max(1, len(quote_tokens)) >= 0.75


def exact_quote_text_matches(quote: str, source_text: str) -> bool:
    return exact_quote_source_span(quote, source_text) is not None


def exact_quote_source_span(quote: str, source_text: str) -> str | None:
    """Return the source's actual matching span for a whitespace-tolerant quote."""
    parts = [part for part in re.split(r"\s+", str(quote or "").strip()) if part]
    if not parts:
        return None
    match = re.search(r"\s+".join(re.escape(part) for part in parts), source_text or "", flags=re.IGNORECASE)
    return (source_text or "")[match.start() : match.end()] if match is not None else None


def hosted_result_file_id(result: dict[str, Any]) -> str | None:
    for key in ("file_id", "fileId", "id"):
        value = result.get(key)
        if value:
            return str(value)
    return None


def hosted_result_text(result: dict[str, Any]) -> str:
    text = result.get("text")
    if isinstance(text, str):
        return text
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or ""))
        return "\n\n".join(part for part in parts if part).strip()
    return str(result.get("snippet") or result.get("quote") or "")


def safe_metadata(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    for key in ("file_id", "filename", "score", "attributes", "result_index"):
        if key in result:
            allowed[key] = result[key]
    return allowed


def uuid_list(values: list[str]) -> list[uuid.UUID]:
    return [uuid.UUID(str(value)) for value in values if uuid_or_none(str(value))]


def retrieval_channel_for_version(version: SourceVersion) -> str:
    if version.kind == SourceRepresentationKind.TRANSCRIPT:
        return EvidenceRetrievalChannel.LOCAL_TRANSCRIPT_SEARCH
    if version.kind in {SourceRepresentationKind.FETCHED_HTML, SourceRepresentationKind.RENDERED_SNAPSHOT}:
        return EvidenceRetrievalChannel.WEB_SNAPSHOT
    return EvidenceRetrievalChannel.LOCAL_CHUNK_SEARCH


def chunk_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.EXTRACTED_TEXT: 0,
        SourceRepresentationKind.TRANSCRIPT: 0,
    }
    return order.get(kind, 100)


def version_can_back_local_window(version: SourceVersion) -> bool:
    if version.status != V2RecordStatus.READY or not version.storage_path:
        return False
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE:
        return _looks_text_readable_path(version.storage_path)
    return version.kind in {
        SourceRepresentationKind.PAGE_MARKED_TEXT,
        SourceRepresentationKind.EXTRACTED_TEXT,
        SourceRepresentationKind.TRANSCRIPT,
        SourceRepresentationKind.RENDERED_SNAPSHOT,
        SourceRepresentationKind.FETCHED_HTML,
    }


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


__all__ = [
    "chunk_version_priority",
    "dedupe",
    "evidence_origin_retrieval_channel",
    "exact_quote_source_span",
    "exact_quote_text_matches",
    "hosted_result_file_id",
    "hosted_result_text",
    "normalize",
    "quote_text_matches_evidence",
    "retrieval_channel_for_version",
    "safe_metadata",
    "unsupported_model_read_selected_as_support",
    "uuid_list",
    "uuid_or_none",
    "version_can_back_local_window",
]
