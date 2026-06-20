from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_v2 import EvidenceItem, EvidenceLocatorType, EvidenceStrength, SourceVersion
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_CITED, EVIDENCE_STATUS_OBSERVED
from app.generation_v2.tools.evidence_utils import _dedupe, _uuid_or_none


_EVIDENCE_PURPOSE_MAX_CHARS = 120
_AGENT_SNIPPET_MAX_CHARS = 1200
_AGENT_METADATA_TEXT_MAX_CHARS = 500
_AGENT_METADATA_KEYS = {
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


async def mark_cited_evidence(db: AsyncSession, *, generation_run_id: uuid.UUID, evidence_ids: list[str]) -> None:
    for evidence_id in _dedupe(evidence_ids):
        if not _uuid_or_none(evidence_id):
            continue
        item = await db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != generation_run_id:
            continue
        item.metadata_ = {
            **(item.metadata_ or {}),
            "usage_status": EVIDENCE_STATUS_CITED,
            "cited": True,
        }
    await db.flush()


async def persist_evidence_item(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    source_version_id: uuid.UUID,
    provider: str | None = None,
    provider_vector_store_id: str | None = None,
    provider_file_id: str | None = None,
    provider_vector_store_file_id: str | None = None,
    response_id: str | None = None,
    tool_call_id: str | None = None,
    retrieval_channel: str,
    query_text: str | None = None,
    purpose: str | None = None,
    search_result_index: int | None = None,
    score: float | None = None,
    snippet_text: str = "",
    normalized_quote_text: str | None = None,
    locator_text: str | None = None,
    locator_type: str = EvidenceLocatorType.UNKNOWN,
    evidence_strength: str = EvidenceStrength.WEAK,
    metadata: dict[str, Any] | None = None,
) -> EvidenceItem:
    snippet = snippet_text.strip()
    snippet_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
    metadata = {"usage_status": EVIDENCE_STATUS_OBSERVED, **(metadata or {})}
    stored_purpose = _truncate_column_text(purpose, _EVIDENCE_PURPOSE_MAX_CHARS)
    if purpose and stored_purpose != purpose:
        metadata["full_purpose"] = purpose
    if "representation_kind" not in metadata:
        source_version = await db.get(SourceVersion, source_version_id)
        if source_version is not None:
            metadata["representation_kind"] = source_version.kind
    existing_result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == generation_run_id,
            EvidenceItem.source_version_id == source_version_id,
            EvidenceItem.snippet_hash == snippet_hash,
            EvidenceItem.retrieval_channel == str(retrieval_channel),
            EvidenceItem.locator_text == locator_text,
            EvidenceItem.query_text == query_text,
            EvidenceItem.purpose == stored_purpose,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        existing.metadata_ = {
            **(existing.metadata_ or {}),
            "usage_status": (existing.metadata_ or {}).get("usage_status") or metadata.get("usage_status"),
            "reused": True,
            "queries": sorted({*(existing.metadata_ or {}).get("queries", []), *([query_text] if query_text else [])}),
        }
        await db.flush()
        return existing
    item = EvidenceItem(
        generation_run_id=generation_run_id,
        project_id=project_id,
        source_id=source_id,
        source_version_id=source_version_id,
        provider=provider,
        provider_vector_store_id=provider_vector_store_id,
        provider_file_id=provider_file_id,
        provider_vector_store_file_id=provider_vector_store_file_id,
        response_id=response_id,
        tool_call_id=tool_call_id,
        retrieval_channel=str(retrieval_channel),
        query_text=query_text,
        purpose=stored_purpose,
        search_result_index=search_result_index,
        score=score,
        snippet_text=snippet,
        normalized_quote_text=normalized_quote_text,
        locator_text=locator_text,
        locator_type=str(locator_type),
        evidence_strength=str(evidence_strength),
        snippet_hash=snippet_hash,
        metadata_=metadata,
    )
    db.add(item)
    await db.flush()
    return item


def _truncate_column_text(value: str | None, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


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


def compact_evidence_payload(item: EvidenceItem, *, max_snippet_chars: int = _AGENT_SNIPPET_MAX_CHARS) -> dict[str, Any]:
    metadata = item.metadata_ or {}
    snippet_text = _truncate_agent_text(item.snippet_text, max_snippet_chars)
    payload: dict[str, Any] = {
        "evidence_id": str(item.id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "representation_kind": metadata.get("representation_kind"),
        "retrieval_channel": item.retrieval_channel,
        "query_text": _truncate_agent_text(item.query_text, 300),
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
    compact_metadata = _compact_agent_metadata(metadata)
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
        "short_snippet": _truncate_agent_text(item.snippet_text, max_short_snippet_chars),
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
            value = _truncate_agent_text(value, _AGENT_METADATA_TEXT_MAX_CHARS)
        if value not in (None, "", []):
            compact[key] = value
    return compact


def compact_provider_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for failure in failures[:4]:
        compact.append(
            {
                key: _truncate_agent_text(str(value), 300) if key == "error" else value
                for key, value in failure.items()
                if key in {"source_id", "source_version_id", "error"} and value not in (None, "", [])
            }
        )
    return compact


def _compact_agent_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in _AGENT_METADATA_KEYS:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if isinstance(value, str):
            value = _truncate_agent_text(value, _AGENT_METADATA_TEXT_MAX_CHARS)
        if value not in (None, "", []):
            compact[key] = value
    return compact


def _truncate_agent_text(value: str | None, max_chars: int) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


__all__ = [name for name in globals() if not name.startswith('__')]
