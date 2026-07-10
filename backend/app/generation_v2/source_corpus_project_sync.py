from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus_helpers import canonical_provider_version as _canonical_provider_version
from app.generation_v2.source_corpus_provider import ensure_project_provider_index, sync_provider_corpus_files
from app.generation_v2.source_corpus_versions import ensure_source_versions
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source


@dataclass(frozen=True)
class SourceCorpusProjectSyncDeps:
    ensure_project_provider_index: Any
    ensure_source_versions: Any
    sync_provider_corpus_files: Any


def default_source_corpus_project_sync_deps() -> SourceCorpusProjectSyncDeps:
    return SourceCorpusProjectSyncDeps(
        ensure_project_provider_index=ensure_project_provider_index,
        ensure_source_versions=ensure_source_versions,
        sync_provider_corpus_files=sync_provider_corpus_files,
    )


async def load_ready_source_versions_for_sources(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_ids: list[uuid.UUID],
) -> list[SourceVersion]:
    if not source_ids:
        return []
    result = await db.execute(
        select(SourceVersion)
        .where(
            SourceVersion.project_id == project_id,
            SourceVersion.source_id.in_(source_ids),
            SourceVersion.status == V2RecordStatus.READY,
        )
        .order_by(SourceVersion.created_at.asc())
    )
    return list(result.scalars())


async def sync_source_to_project_corpus_now(
    db: AsyncSession,
    source: Source,
    *,
    deps: SourceCorpusProjectSyncDeps | None = None,
) -> dict[str, Any]:
    """Sync one source into the project-level V2/OpenAI corpus."""
    deps = deps or default_source_corpus_project_sync_deps()
    source_id = source.id
    project_id = source.project_id
    provider_index = await deps.ensure_project_provider_index(db, project_id)
    await commit_if_supported(db)
    source_result = await db.execute(select(Source).where(Source.id == source_id))
    source = source_result.scalar_one()
    versions = await deps.ensure_source_versions(db, source)
    await commit_if_supported(db)
    active_versions = [version for version in versions if version.is_active and version.status == V2RecordStatus.READY]
    sync_versions = await load_ready_source_versions_for_sources(db, project_id, [source.id])
    provider_files = (
        await deps.sync_provider_corpus_files(db, None, provider_index, sync_versions, source_ids={source.id})
        if provider_index
        else []
    )
    ready_provider_files = [file for file in provider_files if file.status == V2RecordStatus.READY]
    canonical = _canonical_provider_version(active_versions)
    summary = {
        "source_id": str(source.id),
        "source_version_count": len(active_versions),
        "canonical_source_version_id": str(canonical.id) if canonical else None,
        "canonical_kind": str(canonical.kind) if canonical else None,
        "provider_index_id": str(provider_index.id) if provider_index else None,
        "provider_vector_store_id": provider_index.provider_vector_store_id if provider_index else None,
        "provider_file_count": len(provider_files),
        "ready_provider_file_count": len(ready_provider_files),
        "native_uploaded": any(
            version.kind
            in {
                SourceRepresentationKind.ORIGINAL_FILE,
                SourceRepresentationKind.FETCHED_HTML,
                SourceRepresentationKind.MODEL_READ_TEXT,
            }
            for version in active_versions
        ),
        "openai_url_read_status": (source.metadata_ or {}).get("v2_openai_url_read", {}).get("status"),
        "openai_url_recovery_status": (source.metadata_ or {}).get("v2_openai_url_recovery", {}).get("recovery_status"),
    }
    source.metadata_ = {
        **(source.metadata_ or {}),
        "v2_native_ingest": {
            "status": (
                "provider_ready"
                if summary["ready_provider_file_count"]
                else "source_versions_ready"
                if summary["source_version_count"]
                else "empty"
            ),
            "synced_at": datetime.utcnow().isoformat(),
            **summary,
        },
    }
    await db.flush()
    return summary


async def load_project_corpus_sources(db: AsyncSession, project_id: uuid.UUID) -> list[Source]:
    source_result = await db.execute(
        select(Source)
        .where(
            Source.project_id == project_id,
            Source.is_temporary.is_(False),
            or_(
                Source.ingestion_status == "completed",
                Source.file_path.is_not(None),
                Source.original_url.is_not(None),
                Source.raw_text_path.is_not(None),
            ),
        )
        .order_by(Source.created_at.asc())
    )
    return list(source_result.scalars())


async def reconcile_project_corpus_index_now(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    deps: SourceCorpusProjectSyncDeps | None = None,
) -> dict[str, Any]:
    """Repair the provider corpus index from already-materialized source versions."""
    deps = deps or default_source_corpus_project_sync_deps()
    sources = await load_project_corpus_sources(db, project_id)
    source_ids = {source.id for source in sources}
    ready_versions = await load_ready_source_versions_for_sources(db, project_id, list(source_ids))
    active_versions = [version for version in ready_versions if version.is_active]
    provider_index = await deps.ensure_project_provider_index(db, project_id)
    await commit_if_supported(db)
    if not provider_index:
        return {"source_count": len(sources), "source_version_count": len(active_versions), "provider_file_count": 0}
    provider_files = await deps.sync_provider_corpus_files(
        db,
        None,
        provider_index,
        ready_versions,
        source_ids=source_ids,
    )
    await db.flush()
    return {
        "source_count": len(sources),
        "source_version_count": len(active_versions),
        "provider_file_count": len(provider_files),
    }


async def sync_project_corpus_index_now(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    deps: SourceCorpusProjectSyncDeps | None = None,
) -> None:
    deps = deps or default_source_corpus_project_sync_deps()
    sources = await load_project_corpus_sources(db, project_id)
    versions: list[SourceVersion] = []
    for source in sources:
        versions.extend(await deps.ensure_source_versions(db, source))
    await commit_if_supported(db)
    active_versions = [version for version in versions if version.is_active and version.status == V2RecordStatus.READY]
    provider_index = await deps.ensure_project_provider_index(db, project_id)
    await commit_if_supported(db)
    if provider_index:
        await deps.sync_provider_corpus_files(
            db,
            None,
            provider_index,
            active_versions,
            source_ids={source.id for source in sources},
        )
    await db.flush()


async def commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()
