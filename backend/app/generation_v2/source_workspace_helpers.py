from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.generation_v2 import SourceRepresentation, SourceRepresentationKind, V2RecordStatus
from app.models.source import Source
from app.services.storage import download_text, get_minio_client


async def load_source_chunks(db: AsyncSession, source_id: uuid.UUID) -> list[Chunk]:
    result = await db.execute(select(Chunk).where(Chunk.source_id == source_id).order_by(Chunk.chunk_index.asc()).limit(12))
    return list(result.scalars())


async def representation_counts(db: AsyncSession, representation_ids: list[uuid.UUID]) -> tuple[int, int, str]:
    if not representation_ids:
        return 0, 0, "failed"
    result = await db.execute(select(SourceRepresentation).where(SourceRepresentation.id.in_(representation_ids)))
    reps = list(result.scalars())
    ready = len([rep for rep in reps if rep.status == V2RecordStatus.READY])
    failed = len([rep for rep in reps if rep.status == V2RecordStatus.FAILED])
    if ready and not failed:
        return ready, failed, "ready"
    if ready:
        return ready, failed, "partial"
    return ready, failed, "failed"


async def ready_text_representation_id(db: AsyncSession, source_id: uuid.UUID) -> str | None:
    result = await db.execute(
        select(SourceRepresentation)
        .where(
            SourceRepresentation.source_id == source_id,
            SourceRepresentation.status == V2RecordStatus.READY,
            SourceRepresentation.kind.in_([SourceRepresentationKind.EXTRACTED_TEXT, SourceRepresentationKind.TRANSCRIPT]),
        )
        .order_by(SourceRepresentation.created_at.asc())
        .limit(1)
    )
    rep = result.scalar_one_or_none()
    return str(rep.id) if rep else None


async def source_preview(source: Source, chunks: list[Chunk]) -> str:
    if source.text_preview:
        return source.text_preview
    if chunks:
        return "\n\n".join(chunk.content for chunk in chunks[:4])
    if source.raw_text_path:
        try:
            return download_text(get_minio_client(), source.raw_text_path)[:4000]
        except Exception:
            return ""
    return source.title or source.original_url or source.file_path or ""


def source_set_hash(sources: list[Source]) -> str:
    payload = [
        {
            "id": str(source.id),
            "updated": source.status_changed_at.isoformat() if isinstance(source.status_changed_at, datetime) else "",
            "raw_text_path": source.raw_text_path,
            "file_path": source.file_path,
            "url": source.original_url,
        }
        for source in sources
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


__all__ = [
    "load_source_chunks",
    "ready_text_representation_id",
    "representation_counts",
    "source_preview",
    "source_set_hash",
]
