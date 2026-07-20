from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus_helpers import canonical_provider_version as _canonical_provider_version
from app.generation_v2.source_corpus_helpers import provider_file_search_ready as _provider_file_search_ready
from app.generation_v2.source_corpus_content_preflight import assess_run_content_versions
from app.generation_v2.source_corpus_provider import ensure_project_provider_index, sync_provider_corpus_files
from app.generation_v2.source_corpus_provider import mark_provider_files_inactive
from app.generation_v2.source_corpus_versions import ensure_source_versions
from app.generation_v2.source_content_readiness import (
    CONTENT_ASSESSMENT_SCHEMA_VERSION,
    CONTENT_STATE_CONFIRMED,
    content_assessment_metadata,
    source_version_content_state,
    source_version_model_eligible,
)
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source


@dataclass(frozen=True)
class SourceCorpusProjectSyncDeps:
    ensure_project_provider_index: Any
    ensure_source_versions: Any
    sync_provider_corpus_files: Any
    mark_provider_files_inactive: Any = mark_provider_files_inactive


def default_source_corpus_project_sync_deps() -> SourceCorpusProjectSyncDeps:
    return SourceCorpusProjectSyncDeps(
        ensure_project_provider_index=ensure_project_provider_index,
        ensure_source_versions=ensure_source_versions,
        sync_provider_corpus_files=sync_provider_corpus_files,
        mark_provider_files_inactive=mark_provider_files_inactive,
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
    sync_versions = await load_ready_source_versions_for_sources(db, project_id, [source.id])
    _persist_legacy_content_assessments([source], sync_versions)
    await db.flush()
    for version in _disqualified_versions([*versions, *sync_versions]):
        await deps.mark_provider_files_inactive(
            db,
            source_version_id=version.id,
            cleanup_status="target_content_unavailable",
        )
    active_versions = [
        version for version in sync_versions if version.is_active and version.status == V2RecordStatus.READY
    ]
    content_ready_versions = [version for version in active_versions if source_version_model_eligible(version)]
    canonical = _canonical_provider_version(content_ready_versions)
    provider_files = (
        await deps.sync_provider_corpus_files(db, None, provider_index, sync_versions, source_ids={source.id})
        if provider_index
        else []
    )
    ready_provider_files = [
        file
        for file in provider_files
        if canonical is not None
        and file.source_version_id == canonical.id
        and _provider_file_search_ready(file)
    ]
    summary = {
        "source_id": str(source.id),
        "source_version_count": len(active_versions),
        "content_ready_source_version_count": len(content_ready_versions),
        "native_content_ready": canonical is not None,
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
            for version in content_ready_versions
        ),
        "openai_url_read_status": (source.metadata_ or {}).get("v2_openai_url_read", {}).get("status"),
        "openai_url_recovery_status": (source.metadata_ or {}).get("v2_openai_url_recovery", {}).get("recovery_status"),
    }
    source.metadata_ = {
        **(source.metadata_ or {}),
        "v2_native_ingest": {
            "status": (
                "provider_ready"
                if summary["native_content_ready"] and summary["ready_provider_file_count"]
                else "source_versions_ready"
                if summary["native_content_ready"]
                else "target_content_unavailable"
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
    _persist_legacy_content_assessments(sources, ready_versions)
    await db.flush()
    for version in _disqualified_versions(ready_versions):
        await deps.mark_provider_files_inactive(
            db,
            source_version_id=version.id,
            cleanup_status="target_content_unavailable",
        )
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
    materialized_versions: list[SourceVersion] = []
    for source in sources:
        materialized_versions.extend(await deps.ensure_source_versions(db, source))
    await commit_if_supported(db)
    source_ids = {source.id for source in sources}
    versions = await load_ready_source_versions_for_sources(db, project_id, list(source_ids))
    _persist_legacy_content_assessments(sources, versions)
    await db.flush()
    for version in _disqualified_versions([*materialized_versions, *versions]):
        await deps.mark_provider_files_inactive(
            db,
            source_version_id=version.id,
            cleanup_status="target_content_unavailable",
        )
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


def _disqualified_versions(versions: list[SourceVersion]) -> list[SourceVersion]:
    by_id: dict[uuid.UUID, SourceVersion] = {}
    for version in versions:
        if not version.is_active and source_version_content_state(version) != CONTENT_STATE_CONFIRMED:
            by_id[version.id] = version
    return list(by_id.values())


def _persist_legacy_content_assessments(
    sources: list[Source],
    versions: list[SourceVersion],
) -> None:
    assessments = assess_run_content_versions(sources, versions)
    for version in versions:
        assessment = assessments.get(str(version.id))
        if assessment is None:
            continue
        metadata = version.metadata_ if isinstance(version.metadata_, dict) else {}
        persisted = metadata.get("content_assessment")
        if (
            not isinstance(persisted, dict)
            or persisted.get("schema_version") != CONTENT_ASSESSMENT_SCHEMA_VERSION
        ):
            version.metadata_ = {
                **metadata,
                "content_assessment": content_assessment_metadata(assessment),
            }
        if not assessment.confirmed:
            version.is_active = False
