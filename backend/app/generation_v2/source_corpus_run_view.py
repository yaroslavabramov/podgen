from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus_helpers import (
    version_has_snapshot as _version_has_snapshot,
    version_kind_counts as _version_kind_counts,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    ProviderCorpusFile,
    ProviderCorpusIndex,
    RunCorpusView,
    SourceVersion,
    V2RecordStatus,
)
from app.models.source import Source


async def create_run_corpus_view(
    db: AsyncSession,
    run: GenerationRun,
    provider_index: ProviderCorpusIndex | None,
    sources: list[Source],
    source_versions: list[SourceVersion],
    provider_files: list[ProviderCorpusFile],
    *,
    skipped_sources: list[dict[str, Any]] | None = None,
    canonical_source_version_ids: list[str] | None = None,
    total_source_count: int | None = None,
) -> RunCorpusView:
    existing_result = await db.execute(select(RunCorpusView).where(RunCorpusView.generation_run_id == run.id))
    existing = existing_result.scalar_one_or_none()
    selected_source_ids = [str(source.id) for source in sources]
    selected_version_ids = [str(version.id) for version in source_versions]
    eligible_provider_file_ids = [
        file.provider_file_id for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY
    ]
    provider_ready_source_ids = {
        str(file.source_id) for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY
    }
    local_snapshot_source_ids = {str(version.source_id) for version in source_versions if _version_has_snapshot(version)}
    usable_source_ids = set(selected_source_ids)
    canonical_ids = [str(item) for item in (canonical_source_version_ids or [])]
    fallback_ids = [version_id for version_id in selected_version_ids if version_id not in set(canonical_ids)]
    canonical_kind_counts = _version_kind_counts(
        version for version in source_versions if str(version.id) in set(canonical_ids)
    )
    skipped = skipped_sources or []
    readiness = {
        "source_count": len(sources),
        "total_source_count": total_source_count if total_source_count is not None else len(sources) + len(skipped),
        "source_version_count": len(source_versions),
        "provider_file_count": len(eligible_provider_file_ids),
        "provider_index_ready": bool(provider_index and provider_index.status == V2RecordStatus.READY),
        "snapshot_backed_source_count": len(local_snapshot_source_ids),
        "provider_ready_source_count": len(provider_ready_source_ids),
        "usable_source_count": len(usable_source_ids),
        "canonical_source_count": len(canonical_ids),
        "canonical_kind_counts": canonical_kind_counts,
        "skipped_source_count": len(skipped),
        "skipped_sources": skipped,
    }
    status = V2RecordStatus.READY
    if not readiness["source_version_count"] or not readiness["usable_source_count"]:
        status = V2RecordStatus.FAILED
    manifest_payload = {
        "sources": selected_source_ids,
        "versions": selected_version_ids,
        "provider_files": eligible_provider_file_ids,
        "canonical_source_version_ids": canonical_ids,
        "skipped_sources": skipped,
        "provider_index_id": str(provider_index.id) if provider_index else None,
    }
    manifest_hash = hashlib.sha256(json.dumps(manifest_payload, sort_keys=True).encode("utf-8")).hexdigest()
    values = {
        "project_id": run.project_id,
        "provider_index_id": provider_index.id if provider_index else None,
        "selected_source_ids": selected_source_ids,
        "selected_source_version_ids": selected_version_ids,
        "active_source_filter": {
            "source_ids": selected_source_ids,
            "source_version_ids": selected_version_ids,
            "canonical_source_version_ids": canonical_ids,
            "fallback_source_version_ids": fallback_ids,
        },
        "overlay_index_ids": [],
        "eligible_provider_file_ids": eligible_provider_file_ids,
        "readiness_summary": readiness,
        "manifest_hash": manifest_hash,
        "status": status,
    }
    if existing:
        for key, value in values.items():
            setattr(existing, key, value)
        await db.flush()
        if existing.status == V2RecordStatus.FAILED:
            raise RuntimeError("V2 run corpus view has no usable sources")
        return existing
    run_view = RunCorpusView(generation_run_id=run.id, **values)
    db.add(run_view)
    await db.flush()
    if run_view.status == V2RecordStatus.FAILED:
        raise RuntimeError("V2 run corpus view has no usable sources")
    return run_view
