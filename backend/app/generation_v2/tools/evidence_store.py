from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.corpus_scope import session_corpus_scope
from app.models.generation_v2 import EvidenceItem, EvidenceLocatorType, EvidenceStrength, SourceVersion
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_CITED, EVIDENCE_STATUS_OBSERVED
from app.generation_v2.tools.evidence_store_payloads import compact_model_read_result, evidence_payload
from app.generation_v2.tools.evidence_utils import _dedupe, _uuid_or_none


_EVIDENCE_PURPOSE_MAX_CHARS = 120

async def mark_cited_evidence(db: AsyncSession, *, generation_run_id: uuid.UUID, evidence_ids: list[str]) -> None:
    scope = session_corpus_scope(db, generation_run_id)
    for evidence_id in _dedupe(evidence_ids):
        if not _uuid_or_none(evidence_id):
            continue
        item = await db.get(EvidenceItem, uuid.UUID(evidence_id))
        if (
            item is None
            or item.generation_run_id != generation_run_id
            or int(getattr(item, "corpus_revision", 1) or 1) != scope.revision
            or int(getattr(item, "attempt", 1) or 1) != scope.attempt
        ):
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
    scope = session_corpus_scope(db, generation_run_id)
    snippet = snippet_text.strip()
    snippet_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
    metadata = {"usage_status": EVIDENCE_STATUS_OBSERVED, **(metadata or {})}
    stored_purpose = str(purpose) if purpose is not None else None
    if stored_purpose is not None and len(stored_purpose) > _EVIDENCE_PURPOSE_MAX_CHARS:
        raise ValueError(
            "semantic_field_too_long: field=evidence.purpose "
            f"max_length={_EVIDENCE_PURPOSE_MAX_CHARS} actual_length={len(stored_purpose)}"
        )
    if "representation_kind" not in metadata:
        source_version = await db.get(SourceVersion, source_version_id)
        if source_version is not None:
            metadata["representation_kind"] = source_version.kind
    existing_result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == generation_run_id,
            EvidenceItem.corpus_revision == scope.revision,
            EvidenceItem.attempt == scope.attempt,
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
        corpus_revision=scope.revision,
        attempt=scope.attempt,
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
__all__ = [
    "compact_model_read_result",
    "evidence_payload",
    "mark_cited_evidence",
    "persist_evidence_item",
]
