from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.generation_v2 import SourceRepresentation
from app.models.source import Source
from app.services.storage import download_text, get_minio_client


async def search_corpus(db: AsyncSession, project_id: uuid.UUID, query: str, limit: int = 8) -> dict[str, Any]:
    tokens = [token.strip() for token in query.split() if len(token.strip()) >= 3][:6]
    if not tokens:
        return {"results": []}
    clauses = [Chunk.content.ilike(f"%{token}%") for token in tokens]
    result = await db.execute(
        select(Chunk, Source)
        .join(Source, Source.id == Chunk.source_id)
        .where(Source.project_id == project_id, or_(*clauses))
        .limit(80)
    )
    rows = []
    for chunk, source in result.fetchall():
        rows.append(
            {
                "source_id": str(source.id),
                "source_title": source.title or source.original_url or "Untitled",
                "chunk_id": str(chunk.id),
                "heading_path": chunk.heading_path or [],
                "content": chunk.content[:1400],
                "score": _chunk_score(chunk, source, tokens),
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return {"results": rows[: max(1, min(limit, 20))]}


async def fetch_source_view(db: AsyncSession, source_id: uuid.UUID, kind: str | None = None) -> dict[str, Any]:
    query = select(SourceRepresentation).where(SourceRepresentation.source_id == source_id)
    if kind:
        query = query.where(SourceRepresentation.kind == kind)
    result = await db.execute(query.order_by(SourceRepresentation.created_at.desc()).limit(1))
    rep = result.scalar_one_or_none()
    if rep is None:
        return {"found": False}
    text = ""
    if rep.storage_path and rep.kind not in {"original_url", "original_file", "provider_file"}:
        try:
            text = download_text(get_minio_client(), rep.storage_path)[:6000]
        except Exception as exc:
            text = f"[Could not read representation text: {exc}]"
    return {
        "found": True,
        "representation_id": str(rep.id),
        "kind": rep.kind,
        "storage_path": rep.storage_path,
        "provider": rep.provider,
        "provider_file_id": rep.provider_file_id,
        "provider_vector_store_id": rep.provider_vector_store_id,
        "status": rep.status,
        "metadata": rep.metadata_,
        "text": text,
    }


def _chunk_score(chunk: Chunk, source: Source, tokens: list[str]) -> float:
    content = chunk.content.lower()
    score = sum(content.count(token.lower()) for token in tokens) * 2.0
    score += {"primary": 3.0, "secondary": 2.0, "tertiary": 0.8, "unknown": 0.0}.get(source.authority_type, 0.0)
    score += {"high": 2.0, "medium": 1.0, "unknown": 0.0, "low": -1.0}.get(source.domain_reliability, 0.0)
    if source.published_at and source.published_at > datetime.utcnow() - timedelta(days=365 * 5):
        score += 0.5
    score -= min(1.0, chunk.chunk_index * 0.02)
    return round(score, 3)
