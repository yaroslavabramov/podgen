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
from app.generation_v2.corpus_scope import set_session_corpus_scope
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    EvidenceItem,
    ProviderCorpusFile,
    ProviderCorpusIndex,
    RunCorpusView,
    SourceVersion,
    V2Artifact,
    V2ArtifactType,
    V2RecordStatus,
    V2ToolTrace,
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
    selected_source_ids = sorted({str(source.id) for source in sources})
    selected_version_ids = sorted({str(version.id) for version in source_versions})
    eligible_provider_file_ids = sorted({
        file.provider_file_id for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY
    })
    provider_ready_source_ids = {
        str(file.source_id) for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY
    }
    local_snapshot_source_ids = {str(version.source_id) for version in source_versions if _version_has_snapshot(version)}
    usable_source_ids = set(selected_source_ids)
    canonical_ids = sorted({str(item) for item in (canonical_source_version_ids or [])})
    fallback_ids = [version_id for version_id in selected_version_ids if version_id not in set(canonical_ids)]
    canonical_kind_counts = _version_kind_counts(
        version for version in source_versions if str(version.id) in set(canonical_ids)
    )
    skipped = sorted(
        [dict(item) for item in (skipped_sources or [])],
        key=lambda item: (
            str(item.get("source_id") or ""),
            str(item.get("reason") or ""),
            str(item.get("canonical_version_id") or ""),
        ),
    )
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
    # Serialize corpus revisions per run, including the first revision where no
    # RunCorpusView row exists yet.
    await db.execute(
        select(GenerationRun.id)
        .where(GenerationRun.id == run.id)
        .with_for_update()
    )
    existing_result = await db.execute(
        select(RunCorpusView)
        .where(
            RunCorpusView.generation_run_id == run.id,
            RunCorpusView.is_current.is_(True),
        )
        .order_by(RunCorpusView.revision.desc(), RunCorpusView.attempt.desc())
        .limit(1)
        .with_for_update()
    )
    existing = existing_result.scalar_one_or_none()
    if existing is None:
        revision = 1
        # V2 runs created before run_corpus_views existed already have durable
        # revision-1/attempt-1 history. Resume them in a fresh attempt so their
        # partial triage/research data cannot leak into the retry.
        attempt = 2 if await _legacy_v2_history_exists(db, run.id) else 1
    elif existing.manifest_hash == manifest_hash:
        revision = int(existing.revision or 1)
        attempt = int(existing.attempt or 1) + 1
    else:
        revision = int(existing.revision or 1) + 1
        attempt = 1
    values = {
        "project_id": run.project_id,
        "provider_index_id": provider_index.id if provider_index else None,
        "revision": revision,
        "attempt": attempt,
        "is_current": True,
        "supersedes_id": existing.id if existing else None,
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
        existing.is_current = False
        # Clear the partial unique slot before inserting the replacement row.
        await db.flush()
    run_view = RunCorpusView(generation_run_id=run.id, **values)
    db.add(run_view)
    await db.flush()
    set_session_corpus_scope(db, run.id, run_view)
    if run_view.status == V2RecordStatus.FAILED:
        raise RuntimeError("V2 run corpus view has no usable sources")
    return run_view


async def _legacy_v2_history_exists(db: AsyncSession, generation_run_id: Any) -> bool:
    for model in (V2Artifact, V2ToolTrace, EvidenceItem):
        result = await db.execute(
            select(model.id)
            .where(model.generation_run_id == generation_run_id)
            .limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return True
    return False


async def current_attempt_has_triage_history(
    db: AsyncSession,
    run_view: RunCorpusView,
) -> bool:
    """Return whether triage already wrote durable state in this corpus attempt."""
    result = await db.execute(
        select(V2Artifact.id)
        .where(
            V2Artifact.generation_run_id == run_view.generation_run_id,
            V2Artifact.corpus_revision == int(run_view.revision or 1),
            V2Artifact.attempt == int(run_view.attempt or 1),
            V2Artifact.artifact_type.in_(
                [
                    V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
                    V2ArtifactType.SOURCE_TRIAGE,
                    V2ArtifactType.SOURCE_MAP,
                ]
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


__all__ = ["create_run_corpus_view", "current_attempt_has_triage_history"]
