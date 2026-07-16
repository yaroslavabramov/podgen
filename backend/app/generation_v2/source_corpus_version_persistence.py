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
    # Version replacement is a read-modify-write operation. Serialize it per
    # source so duplicate sync workers cannot leave multiple competing current
    # versions of the same representation kind.
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        await db.execute(select(Source.id).where(Source.id == source.id).with_for_update())
    result = await db.execute(
        select(SourceVersion)
        .where(
            SourceVersion.source_id == source.id,
            SourceVersion.kind == kind,
            SourceVersion.content_hash == content_hash,
            SourceVersion.storage_path == storage_path,
        )
        .order_by(SourceVersion.created_at.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.metadata_ = {**(existing.metadata_ or {}), **metadata}
        if status != V2RecordStatus.READY:
            # Content assessment is derived metadata. Keep the immutable
            # historical row/status readable for pinned completed runs, but
            # remove a newly-disqualified surface from every future corpus.
            existing.is_active = False
            await db.flush()
            return existing
        if existing.status == V2RecordStatus.READY and existing.is_active:
            await db.flush()
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
        supersedes_source_version_id=previous.id if previous and status == V2RecordStatus.READY else None,
        # A failed materialization is immutable history, not the new current
        # retrieval surface. Keep the last ready version active for this source.
        is_active=status == V2RecordStatus.READY,
    )
    db.add(version)
    await db.flush()
    if previous and status == V2RecordStatus.READY:
        previous.is_active = False
        version.supersedes_source_version_id = previous.id
        await db.flush()
    return version


__all__ = ["ensure_source_version"]
