from __future__ import annotations

from typing import Any

from app.models.generation_v2 import EvidenceItem

AGENT_SNIPPET_MAX_CHARS = 1200
AGENT_METADATA_TEXT_MAX_CHARS = 500
AGENT_METADATA_KEYS = {
    "usage_status",
    "supports_query",
    "support_level",
    "selected_purpose",
    "selected_role",
    "selected_rationale",
    "model_file_read",
    "model_file_read_local_fallback",
    "source_read",
    "source_internal_search",
    "source_skim",
    "hosted_file_search",
    "fallback",
    "source_version_fallback",
    "requested_locator",
    "local_read_channel",
    "source_version_kind",
}


def evidence_payload(item: EvidenceItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "representation_kind": (item.metadata_ or {}).get("representation_kind"),
        "provider": item.provider,
        "provider_file_id": item.provider_file_id,
        "provider_vector_store_id": item.provider_vector_store_id,
        "provider_vector_store_file_id": item.provider_vector_store_file_id,
        "retrieval_channel": item.retrieval_channel,
        "query_text": item.query_text,
        "purpose": item.purpose,
        "score": item.score,
        "snippet_text": item.snippet_text,
        "normalized_quote_text": item.normalized_quote_text,
        "locator_text": item.locator_text,
        "locator_type": item.locator_type,
        "evidence_strength": item.evidence_strength,
        "snippet_hash": item.snippet_hash,
        "metadata": item.metadata_,
    }


def compact_evidence_payload(item: EvidenceItem, *, max_snippet_chars: int = AGENT_SNIPPET_MAX_CHARS) -> dict[str, Any]:
    metadata = item.metadata_ or {}
    snippet_text = truncate_agent_text(item.snippet_text, max_snippet_chars)
    payload: dict[str, Any] = {
        "evidence_id": str(item.id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "representation_kind": metadata.get("representation_kind"),
        "retrieval_channel": item.retrieval_channel,
        "query_text": truncate_agent_text(item.query_text, 300),
        "purpose": item.purpose,
        "score": item.score,
        "snippet_text": snippet_text,
        "locator_text": item.locator_text,
        "locator_type": item.locator_type,
        "evidence_strength": item.evidence_strength,
        "usage_status": metadata.get("usage_status"),
    }
    if item.snippet_text and snippet_text != item.snippet_text:
        payload["snippet_original_chars"] = len(item.snippet_text)
        payload["snippet_returned_chars"] = len(snippet_text)
        payload["snippet_complete"] = False
    compact_metadata = compact_agent_metadata(metadata)
    if compact_metadata:
        payload["metadata"] = compact_metadata
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def evidence_preview_payload(item: EvidenceItem, *, max_short_snippet_chars: int = 700) -> dict[str, Any]:
    metadata = item.metadata_ or {}
    payload: dict[str, Any] = {
        "evidence_id": str(item.id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "representation_kind": metadata.get("representation_kind"),
        "retrieval_channel": item.retrieval_channel,
        "evidence_strength": item.evidence_strength,
        "locator": item.locator_text,
        "locator_type": item.locator_type,
        "short_snippet": truncate_agent_text(item.snippet_text, max_short_snippet_chars),
        "score": item.score,
        "usage_status": metadata.get("usage_status"),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def compact_model_read_result(read_result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "supports_query",
        "support_level",
        "locator_text",
        "locator_type",
        "confidence",
    }
    compact: dict[str, Any] = {}
    for key in allowed:
        if key not in read_result:
            continue
        value = read_result.get(key)
        if isinstance(value, str):
            value = truncate_agent_text(value, AGENT_METADATA_TEXT_MAX_CHARS)
        if value not in (None, "", []):
            compact[key] = value
    return compact


def compact_provider_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for failure in failures[:4]:
        compact.append(
            {
                key: truncate_agent_text(str(value), 300) if key == "error" else value
                for key, value in failure.items()
                if key in {"source_id", "source_version_id", "error"} and value not in (None, "", [])
            }
        )
    return compact


def compact_agent_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in AGENT_METADATA_KEYS:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if isinstance(value, str):
            value = truncate_agent_text(value, AGENT_METADATA_TEXT_MAX_CHARS)
        if value not in (None, "", []):
            compact[key] = value
    return compact


def truncate_agent_text(value: str | None, max_chars: int) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


__all__ = [
    "compact_agent_metadata",
    "compact_evidence_payload",
    "compact_model_read_result",
    "compact_provider_failures",
    "evidence_payload",
    "evidence_preview_payload",
    "truncate_agent_text",
]
