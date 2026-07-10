from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_v2 import SourceVersion, V2RecordStatus
from app.models.source import Source


async def ensure_source_version(
    db: AsyncSession,
    source: Source,
    *,
    kind: str,
    storage_path: str | None,
    content_hash: str | None,
    metadata: dict[str, Any],
    status: str = V2RecordStatus.READY,
) -> SourceVersion:
    result = await db.execute(
        select(SourceVersion).where(
            SourceVersion.source_id == source.id,
            SourceVersion.kind == kind,
            SourceVersion.content_hash == content_hash,
            SourceVersion.storage_path == storage_path,
            SourceVersion.is_active.is_(True),
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    previous_result = await db.execute(
        select(SourceVersion)
        .where(SourceVersion.source_id == source.id, SourceVersion.kind == kind, SourceVersion.is_active.is_(True))
        .order_by(SourceVersion.created_at.desc())
        .limit(1)
    )
    previous = previous_result.scalar_one_or_none()
    version = SourceVersion(
        source_id=source.id,
        project_id=source.project_id,
        kind=str(kind),
        storage_path=storage_path,
        content_hash=content_hash,
        upload_status=V2RecordStatus.PENDING,
        status=status,
        metadata_=metadata,
        supersedes_source_version_id=previous.id if previous else None,
        is_active=True,
    )
    db.add(version)
    await db.flush()
    if previous:
        previous.is_active = False
        version.supersedes_source_version_id = previous.id
        await db.flush()
    return version


__all__ = ["ensure_source_version"]
