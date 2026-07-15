from __future__ import annotations

import hashlib
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.corpus_scope import session_corpus_scope
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalEvent,
    EvidenceStrength,
    SourceVersion,
)
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_CITED, EVIDENCE_STATUS_OBSERVED
from app.generation_v2.tools.evidence_store_payloads import compact_model_read_result, evidence_payload
from app.generation_v2.tools.evidence_utils import _dedupe, _uuid_or_none
from app.generation_v2.tools.evidence_window_refs import WindowRefError, parse_window_ref


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
    retrieval_metadata = dict(metadata or {})
    snippet_hash = canonical_evidence_hash(
        snippet_text=snippet,
        normalized_quote_text=normalized_quote_text,
        locator_text=locator_text,
        locator_type=str(locator_type),
        evidence_strength=str(evidence_strength),
        metadata=retrieval_metadata,
    )
    metadata = {"usage_status": EVIDENCE_STATUS_OBSERVED, **retrieval_metadata}
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
    existing = await _find_canonical_evidence(
        db,
        generation_run_id=generation_run_id,
        corpus_revision=scope.revision,
        attempt=scope.attempt,
        source_version_id=source_version_id,
        snippet_hash=snippet_hash,
    )
    if existing:
        return await _reuse_canonical_evidence(
            db,
            evidence=existing,
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
            locator_text=locator_text,
            locator_type=str(locator_type),
            normalized_quote_text=normalized_quote_text,
            evidence_strength=str(evidence_strength),
            retrieval_metadata=retrieval_metadata,
        )
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
    if hasattr(db, "begin_nested"):
        try:
            async with db.begin_nested():
                db.add(item)
                await db.flush()
        except IntegrityError:
            # Another worker inserted the same canonical fragment after our lookup.
            # The savepoint keeps the caller's transaction usable and lets us attach
            # this retrieval to the winner instead of creating a duplicate.
            existing = await _find_canonical_evidence(
                db,
                generation_run_id=generation_run_id,
                corpus_revision=scope.revision,
                attempt=scope.attempt,
                source_version_id=source_version_id,
                snippet_hash=snippet_hash,
            )
            if existing is None:
                raise
            return await _reuse_canonical_evidence(
                db,
                evidence=existing,
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
                locator_text=locator_text,
                locator_type=str(locator_type),
                normalized_quote_text=normalized_quote_text,
                evidence_strength=str(evidence_strength),
                retrieval_metadata=retrieval_metadata,
            )
    else:
        # Lightweight test doubles do not implement transaction savepoints.
        db.add(item)
        await db.flush()
    await _record_retrieval_event(
        db,
        evidence=item,
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
        locator_text=locator_text,
        locator_type=str(locator_type),
        metadata={**retrieval_metadata, "evidence_strength": str(evidence_strength), "reused": False},
    )
    await db.flush()
    return item


async def _find_canonical_evidence(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    corpus_revision: int,
    attempt: int,
    source_version_id: uuid.UUID,
    snippet_hash: str,
) -> EvidenceItem | None:
    result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == generation_run_id,
            EvidenceItem.corpus_revision == corpus_revision,
            EvidenceItem.attempt == attempt,
            EvidenceItem.source_version_id == source_version_id,
            EvidenceItem.snippet_hash == snippet_hash,
        )
    )
    return result.scalar_one_or_none()


async def _reuse_canonical_evidence(
    db: AsyncSession,
    *,
    evidence: EvidenceItem,
    generation_run_id: uuid.UUID,
    corpus_revision: int,
    attempt: int,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    source_version_id: uuid.UUID,
    provider: str | None,
    provider_vector_store_id: str | None,
    provider_file_id: str | None,
    provider_vector_store_file_id: str | None,
    response_id: str | None,
    tool_call_id: str | None,
    retrieval_channel: str,
    query_text: str | None,
    purpose: str | None,
    search_result_index: int | None,
    score: float | None,
    locator_text: str | None,
    locator_type: str,
    normalized_quote_text: str | None,
    evidence_strength: str,
    retrieval_metadata: dict[str, Any],
) -> EvidenceItem:
    evidence.metadata_ = {
        **(evidence.metadata_ or {}),
        "usage_status": (evidence.metadata_ or {}).get("usage_status") or EVIDENCE_STATUS_OBSERVED,
        "reused": True,
    }
    if _strength_rank(evidence_strength) > _strength_rank(evidence.evidence_strength):
        evidence.evidence_strength = evidence_strength
        evidence.locator_text = locator_text or evidence.locator_text
        evidence.locator_type = str(locator_type or evidence.locator_type)
        evidence.normalized_quote_text = normalized_quote_text or evidence.normalized_quote_text
    if score is not None and (evidence.score is None or score > evidence.score):
        evidence.score = score
    await _record_retrieval_event(
        db,
        evidence=evidence,
        generation_run_id=generation_run_id,
        corpus_revision=corpus_revision,
        attempt=attempt,
        project_id=project_id,
        source_id=source_id,
        source_version_id=source_version_id,
        provider=provider,
        provider_vector_store_id=provider_vector_store_id,
        provider_file_id=provider_file_id,
        provider_vector_store_file_id=provider_vector_store_file_id,
        response_id=response_id,
        tool_call_id=tool_call_id,
        retrieval_channel=retrieval_channel,
        query_text=query_text,
        purpose=purpose,
        search_result_index=search_result_index,
        score=score,
        locator_text=locator_text,
        locator_type=locator_type,
        metadata={**retrieval_metadata, "evidence_strength": evidence_strength, "reused": True},
    )
    await db.flush()
    return evidence


async def _record_retrieval_event(
    db: AsyncSession,
    *,
    evidence: EvidenceItem,
    generation_run_id: uuid.UUID,
    corpus_revision: int,
    attempt: int,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    source_version_id: uuid.UUID,
    provider: str | None,
    provider_vector_store_id: str | None,
    provider_file_id: str | None,
    provider_vector_store_file_id: str | None,
    response_id: str | None,
    tool_call_id: str | None,
    retrieval_channel: str,
    query_text: str | None,
    purpose: str | None,
    search_result_index: int | None,
    score: float | None,
    locator_text: str | None,
    locator_type: str,
    metadata: dict[str, Any],
) -> None:
    db.add(
        EvidenceRetrievalEvent(
            evidence_id=evidence.id,
            generation_run_id=generation_run_id,
            corpus_revision=corpus_revision,
            attempt=attempt,
            project_id=project_id,
            source_id=source_id,
            source_version_id=source_version_id,
            provider=provider,
            provider_vector_store_id=provider_vector_store_id,
            provider_file_id=provider_file_id,
            provider_vector_store_file_id=provider_vector_store_file_id,
            response_id=response_id,
            tool_call_id=tool_call_id,
            retrieval_channel=retrieval_channel,
            query_text=query_text,
            purpose=purpose,
            search_result_index=search_result_index,
            score=score,
            locator_text=locator_text,
            locator_type=locator_type,
            metadata_=metadata,
        )
    )


def _strength_rank(value: str) -> int:
    return {
        str(EvidenceStrength.WEAK): 0,
        str(EvidenceStrength.SOURCE_LEVEL): 1,
        str(EvidenceStrength.SNIPPET): 2,
        str(EvidenceStrength.EXACT): 3,
    }.get(str(value), 0)


def canonical_evidence_hash(
    *,
    snippet_text: str,
    normalized_quote_text: str | None,
    locator_text: str | None,
    locator_type: str,
    evidence_strength: str,
    metadata: dict[str, Any] | None,
) -> str:
    canonical_text = str(normalized_quote_text or "") if str(evidence_strength) == str(EvidenceStrength.EXACT) else ""
    if not canonical_text.strip():
        canonical_text = str(snippet_text or "")
    canonical_text = " ".join(unicodedata.normalize("NFKC", canonical_text).split())
    location_key = _canonical_location_key(
        locator_text=locator_text,
        locator_type=locator_type,
        evidence_strength=evidence_strength,
        metadata=metadata or {},
    )
    value = canonical_text if not location_key else f"{canonical_text}\x00location:{location_key}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_location_key(
    *,
    locator_text: str | None,
    locator_type: str,
    evidence_strength: str,
    metadata: dict[str, Any],
) -> str:
    if str(evidence_strength) != str(EvidenceStrength.EXACT):
        return ""
    window_ref = str(metadata.get("window_ref") or "").strip()
    if window_ref:
        try:
            window = parse_window_ref(window_ref)
        except WindowRefError:
            pass
        else:
            return (
                f"window:{window.source_version_id}:{window.start_offset}:{window.end_offset}:{window.text_fingerprint}"
            )
    locator = " ".join(unicodedata.normalize("NFKC", str(locator_text or "")).split()).casefold()
    locator_kind = str(locator_type or EvidenceLocatorType.UNKNOWN).casefold()
    return f"locator:{locator_kind}:{locator}" if locator else ""


__all__ = [
    "compact_model_read_result",
    "canonical_evidence_hash",
    "evidence_payload",
    "mark_cited_evidence",
    "persist_evidence_item",
]
