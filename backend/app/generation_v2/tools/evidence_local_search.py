from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select

from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_utils import _normalize, _query_tokens, _uuid_list, _uuid_or_none
from app.models.chunk import Chunk
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    SourceVersion,
    V2RecordStatus,
)


def _source_version_text(version: SourceVersion) -> str:
    from app.generation_v2.tools import evidence_retrieval

    return evidence_retrieval._source_version_text(version)


async def local_fallback_search(
    owner: Any,
    *,
    query: str,
    purpose: str,
    source_ids: list[str],
    limit: int,
) -> list[EvidenceItem]:
    if limit <= 0:
        return []
    tokens = _query_tokens(query)[:8]
    allowed_sources = set(source_ids or [str(item) for item in owner.run_view.selected_source_ids])
    if not tokens:
        return await owner._source_version_fallback(query=query, purpose=purpose, source_ids=allowed_sources, limit=limit)
    clauses = [Chunk.content.ilike(f"%{token}%") for token in tokens]
    result = await owner.db.execute(
        select(Chunk)
        .where(
            Chunk.source_id.in_([uuid.UUID(source_id) for source_id in allowed_sources if _uuid_or_none(source_id)]),
            or_(*clauses),
        )
        .limit(max(20, limit * 4))
    )
    chunk_versions = await owner._chunk_backed_versions_by_source(allowed_sources)
    candidates = []
    for chunk in result.scalars():
        version = chunk_versions.get(str(chunk.source_id))
        if version is None:
            continue
        score = sum(chunk.content.lower().count(token) for token in tokens)
        candidates.append((score, chunk, version))
    candidates.sort(key=lambda item: item[0], reverse=True)
    evidence: list[EvidenceItem] = []
    for idx, (score, chunk, version) in enumerate(candidates[:limit]):
        locator = " > ".join(chunk.heading_path or []) or f"chunk {chunk.chunk_index}"
        channel = (
            EvidenceRetrievalChannel.LOCAL_TRANSCRIPT_SEARCH
            if version.kind == "transcript"
            else EvidenceRetrievalChannel.LOCAL_CHUNK_SEARCH
        )
        evidence.append(
            await persist_evidence_item(
                owner.db,
                generation_run_id=owner.run.id,
                project_id=owner.run.project_id,
                source_id=version.source_id,
                source_version_id=version.id,
                provider=None,
                retrieval_channel=channel,
                query_text=query,
                purpose=purpose,
                search_result_index=idx,
                score=float(score),
                snippet_text=chunk.content[:1600],
                normalized_quote_text=_normalize(chunk.content[:500]),
                locator_text=locator,
                locator_type=EvidenceLocatorType.SECTION if chunk.heading_path else EvidenceLocatorType.UNKNOWN,
                evidence_strength=EvidenceStrength.EXACT if chunk.heading_path else EvidenceStrength.SNIPPET,
                metadata={"chunk_id": str(chunk.id), "fallback": True},
            )
        )
    if len(evidence) < limit:
        evidence.extend(
            await owner._source_version_fallback(
                query=query,
                purpose=purpose,
                source_ids=allowed_sources,
                limit=limit - len(evidence),
            )
        )
    return evidence


async def source_version_fallback(
    owner: Any,
    *,
    query: str,
    purpose: str,
    source_ids: set[str],
    limit: int,
) -> list[EvidenceItem]:
    selected_version_ids = _uuid_list(owner.run_view.selected_source_version_ids)
    result = await owner.db.execute(
        select(SourceVersion)
        .where(
            SourceVersion.is_active.is_(True),
            SourceVersion.status == V2RecordStatus.READY,
            SourceVersion.source_id.in_([uuid.UUID(source_id) for source_id in source_ids if _uuid_or_none(source_id)]),
            SourceVersion.id.in_(selected_version_ids),
        )
        .limit(limit * 3)
    )
    evidence: list[EvidenceItem] = []
    for idx, version in enumerate(result.scalars()):
        if len(evidence) >= limit:
            break
        text = _source_version_text(version)
        if not text.strip():
            continue
        evidence.append(
            await persist_evidence_item(
                owner.db,
                generation_run_id=owner.run.id,
                project_id=owner.run.project_id,
                source_id=version.source_id,
                source_version_id=version.id,
                provider=None,
                retrieval_channel=(
                    EvidenceRetrievalChannel.WEB_SNAPSHOT
                    if version.kind in {"fetched_html", "rendered_snapshot"}
                    else EvidenceRetrievalChannel.LOCAL_CHUNK_SEARCH
                ),
                query_text=query,
                purpose=purpose,
                search_result_index=idx,
                score=0.0,
                snippet_text=text[:1600],
                normalized_quote_text=_normalize(text[:500]),
                locator_text="source snapshot",
                locator_type=EvidenceLocatorType.SOURCE_LEVEL,
                evidence_strength=EvidenceStrength.SOURCE_LEVEL,
                metadata={"source_version_fallback": True},
            )
        )
    return evidence


__all__ = ["local_fallback_search", "source_version_fallback"]
