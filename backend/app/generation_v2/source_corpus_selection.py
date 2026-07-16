from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus_contracts import CorpusProvider
from app.generation_v2.source_content_readiness import source_version_model_eligible
from app.generation_v2.source_corpus_helpers import (
    canonical_provider_version as _canonical_provider_version,
    provider_file_search_ready as _provider_file_search_ready,
    versions_by_source as _versions_by_source,
)
from app.generation_v2.source_corpus_provider import _provider_uploads_required
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    ProviderCorpusFile,
    ProviderCorpusIndex,
    ProviderCorpusScope,
    SourceVersion,
    V2RecordStatus,
)
from app.models.source import Source


async def load_project_provider_index(db: AsyncSession, project_id: uuid.UUID) -> ProviderCorpusIndex | None:
    result = await db.execute(
        select(ProviderCorpusIndex).where(
            ProviderCorpusIndex.project_id == project_id,
            ProviderCorpusIndex.provider == CorpusProvider.OPENAI,
            ProviderCorpusIndex.scope == ProviderCorpusScope.PROJECT,
        )
    )
    return result.scalar_one_or_none()


async def load_active_ready_source_versions(
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


async def load_ready_provider_files(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex | None,
    source_version_ids: list[uuid.UUID],
) -> list[ProviderCorpusFile]:
    if not provider_index or not source_version_ids:
        return []
    result = await db.execute(
        select(ProviderCorpusFile).where(
            ProviderCorpusFile.provider_index_id == provider_index.id,
            ProviderCorpusFile.source_version_id.in_(source_version_ids),
            ProviderCorpusFile.status == V2RecordStatus.READY,
        )
    )
    return list(result.scalars())


def select_ready_run_corpus(
    sources: list[Source],
    source_versions: list[SourceVersion],
    provider_files: list[ProviderCorpusFile],
    provider_index: ProviderCorpusIndex | None,
    *,
    content_rejections: dict[str, list[str]] | None = None,
) -> tuple[list[Source], list[SourceVersion], list[ProviderCorpusFile], list[dict[str, Any]], list[str]]:
    content_rejections = content_rejections or {}
    versions_by_source = _versions_by_source(source_versions)
    provider_by_version = {file.source_version_id: file for file in provider_files if _provider_file_search_ready(file)}
    provider_required = _provider_uploads_required()
    provider_index_ready = bool(
        provider_index and provider_index.provider_vector_store_id and provider_index.status == V2RecordStatus.READY
    )
    usable_sources: list[Source] = []
    usable_versions: list[SourceVersion] = []
    canonical_provider_files: list[ProviderCorpusFile] = []
    canonical_version_ids: list[str] = []
    skipped_sources: list[dict[str, Any]] = []
    for source in sources:
        versions = versions_by_source.get(source.id, [])
        if not versions:
            skipped_sources.append(skipped_source(source, "no_ready_source_versions"))
            continue
        active_versions = [
            version
            for version in versions
            if version.is_active
            and source_version_model_eligible(version)
            and str(version.id) not in content_rejections
        ]
        if not active_versions:
            reasons = sorted(
                {
                    reason
                    for version in versions
                    for reason in content_rejections.get(str(version.id), [])
                    if reason
                }
            )
            skipped_sources.append(
                skipped_source(
                    source,
                    "target_content_unavailable",
                    reason_codes=reasons,
                )
            )
            continue
        canonical = _canonical_provider_version(active_versions)
        if canonical is None:
            provider_file = None
        else:
            provider_file = provider_by_version.get(canonical.id)
        selected_versions = active_versions
        if provider_required:
            if not provider_index_ready:
                skipped_sources.append(skipped_source(source, "provider_index_not_ready"))
                continue
            if provider_file is None:
                # A replacement version becomes active before its external
                # upload is committed. Keep the last provider-ready immutable
                # version usable until that replacement is actually searchable.
                fallback_versions = [
                    version
                    for version in versions
                    if version.id in provider_by_version
                    and source_version_model_eligible(version)
                    and str(version.id) not in content_rejections
                ]
                fallback = _canonical_provider_version(fallback_versions)
                if fallback is None:
                    skipped_sources.append(
                        skipped_source(
                            source,
                            "canonical_provider_file_not_ready",
                            canonical_version_id=str(canonical.id) if canonical else None,
                        )
                    )
                    continue
                canonical = fallback
                provider_file = provider_by_version[fallback.id]
                selected_versions = [fallback]
            canonical_provider_files.append(provider_file)
        elif provider_file is not None:
            canonical_provider_files.append(provider_file)
        if canonical is None:
            skipped_sources.append(skipped_source(source, "no_canonical_source_version"))
            continue
        usable_sources.append(source)
        usable_versions.extend(selected_versions)
        canonical_version_ids.append(str(canonical.id))
    return usable_sources, usable_versions, canonical_provider_files, skipped_sources, canonical_version_ids


def skipped_source(
    source: Source,
    reason: str,
    *,
    canonical_version_id: str | None = None,
    reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_id": str(source.id),
        "reason": reason,
        "source_type": str(source.source_type),
    }
    if canonical_version_id:
        payload["canonical_version_id"] = canonical_version_id
    if reason_codes:
        payload["reason_codes"] = list(reason_codes)
    return payload


async def load_run_sources(db: AsyncSession, run: GenerationRun) -> list[Source]:
    attached_source_ids = set(str(value) for value in (run.config or {}).get("source_ids", []) if value)
    query = select(Source).where(Source.project_id == run.project_id)
    if attached_source_ids:
        query = query.where(Source.id.cast(String).in_(sorted(attached_source_ids)))
    else:
        query = query.where(Source.is_temporary.is_(False))
    query = query.order_by(Source.created_at.asc())
    result = await db.execute(query)
    return list(result.scalars())


__all__ = [
    "load_active_ready_source_versions",
    "load_project_provider_index",
    "load_ready_provider_files",
    "load_run_sources",
    "select_ready_run_corpus",
    "skipped_source",
]
