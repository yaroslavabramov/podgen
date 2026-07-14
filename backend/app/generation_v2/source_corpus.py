from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from app.generation_v2.corpus_scope import scope_for_run_view, session_corpus_scope_or_none
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_tool_trace
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    ProviderCorpusFile,
    ProviderCorpusIndex,
    RunCorpusView,
    SourceVersion,
)
from app.models.source import IngestionStatus, Source
from app.generation_v2.source_corpus_helpers import (
    agent_facing_provider_versions as _agent_facing_provider_versions,
    canonical_provider_version as _canonical_provider_version,
    evidence_to_dict as _evidence_to_dict,
    filename_from_url as _filename_from_url,
    is_pdf_response as _is_pdf_response,
    provider_attributes as _provider_attributes,
    version_has_snapshot as _version_has_snapshot,
)
from app.generation_v2.source_corpus_selection import (
    load_active_ready_source_versions as _load_active_ready_source_versions,
    load_project_provider_index as _load_project_provider_index,
    load_ready_provider_files as _load_ready_provider_files,
    load_run_sources as _load_run_sources,
    select_ready_run_corpus as _select_ready_run_corpus,
    skipped_source as _skipped_source,
)
from app.generation_v2.source_corpus_versions import (
    OpenAIUrlReadVersionDeps,
    call_openai_url_file_read_with_client as _call_openai_url_file_read,
    call_openai_url_recovery_with_client as _call_openai_url_recovery,
    default_openai_url_read_version_deps,
    ensure_openai_url_read_versions as _ensure_openai_url_read_versions,
    ensure_source_version as _ensure_source_version,
    ensure_source_versions,
    ensure_web_snapshot_versions as _ensure_web_snapshot_versions,
    fetch_url_for_v2_snapshot as _fetch_url_for_v2_snapshot,
)
from app.generation_v2.source_corpus_provider import (
    _detach_openai_vector_store_file as _detach_openai_vector_store_file,
    _detach_excluded_provider_files as _detach_excluded_provider_files,
    _detach_provider_file_from_active_index as _detach_provider_file_from_active_index,
    _ensure_provider_file as _ensure_provider_file,
    _ensure_vector_store_file_attached as _ensure_vector_store_file_attached,
    _list_vector_store_files as _list_vector_store_files,
    _poll_provider_file_until_terminal as _poll_provider_file_until_terminal,
    _provider_uploads_required,
    _provider_vector_operation_timeout_seconds as _provider_vector_operation_timeout_seconds,
    _retrieve_vector_store_file as _retrieve_vector_store_file,
    _sync_provider_file_attributes as _sync_provider_file_attributes,
    ensure_project_provider_index,
    SourceCorpusProviderDetachDeps,
    default_source_corpus_provider_detach_deps,
    SourceCorpusProviderUploadDeps,
    default_source_corpus_provider_upload_deps,
    SourceCorpusProviderSyncDeps,
    default_source_corpus_provider_sync_deps,
    mark_provider_files_inactive as mark_provider_files_inactive,
    reconcile_provider_corpus_files as reconcile_provider_corpus_files,
    sync_provider_corpus_files,
)
from app.generation_v2.source_corpus_project_sync import (
    SourceCorpusProjectSyncDeps,
    commit_if_supported as _commit_if_supported,
    default_source_corpus_project_sync_deps,
    reconcile_project_corpus_index_now,
    sync_project_corpus_index_now,
    sync_source_to_project_corpus_now,
)
from app.generation_v2.source_corpus_reconciliation import (
    SourceCorpusReconciliationDeps,
    default_source_corpus_reconciliation_deps,
)
from app.generation_v2.source_corpus_run_view import create_run_corpus_view
from app.generation_v2.source_corpus_summary import project_corpus_index_summary as project_corpus_index_summary
from app.generation_v2.source_corpus_url_read import (
    OPENAI_URL_READ_SCHEMA,
    OPENAI_URL_RECOVERY_SCHEMA,
    format_openai_url_read_document,
    loads_json_object,
)
from app.generation_v2.source_corpus_upload_payload import version_upload_payload as _version_upload_payload
from app.services.llm import get_openai_client
from app.services.storage import download_file, get_minio_client, upload_file, upload_text

logger = logging.getLogger(__name__)


@dataclass
class CorpusPreparationResult:
    run_view: RunCorpusView
    provider_index: ProviderCorpusIndex | None
    sources: list[Source]
    source_versions: list[SourceVersion]
    provider_files: list[ProviderCorpusFile]
    vector_store_id: str | None
    readiness_summary: dict[str, Any]


async def prepare_v2_corpus(db: AsyncSession, run: GenerationRun) -> CorpusPreparationResult:
    sources = await _load_run_sources(db, run)
    explicitly_selected_source_ids = {
        str(value) for value in (run.config or {}).get("source_ids", []) if value
    }
    loaded_source_ids = {str(source.id) for source in sources}
    missing_selected_source_ids = sorted(explicitly_selected_source_ids - loaded_source_ids)
    if missing_selected_source_ids:
        raise RuntimeError(
            "V2 selected sources are missing or outside the project: "
            + ", ".join(missing_selected_source_ids)
        )
    selected_source_ids = explicitly_selected_source_ids or loaded_source_ids
    invalid_selected_sources = [
        source
        for source in sources
        if str(source.id) in selected_source_ids
        and (bool(source.is_temporary) or str(source.ingestion_status) == str(IngestionStatus.FAILED))
    ]
    if invalid_selected_sources:
        details = ", ".join(
            f"{source.id} ({'staged' if source.is_temporary else 'failed'})"
            for source in invalid_selected_sources
        )
        raise RuntimeError(f"V2 selected sources are not ready: {details}")
    if not sources:
        raise RuntimeError("V2 corpus has no eligible project sources")

    provider_index = await _load_project_provider_index(db, run.project_id)
    all_versions = await _load_active_ready_source_versions(db, run.project_id, [source.id for source in sources])
    provider_files = await _load_ready_provider_files(db, provider_index, [version.id for version in all_versions])
    usable_sources, usable_versions, canonical_provider_files, skipped_sources, canonical_version_ids = _select_ready_run_corpus(
        sources,
        all_versions,
        provider_files,
        provider_index,
    )
    unready_selected_sources = [
        item for item in skipped_sources if str(item.get("source_id") or "") in selected_source_ids
    ]
    if unready_selected_sources:
        details = ", ".join(
            f"{item['source_id']} ({item.get('reason') or 'not_ready'})"
            for item in unready_selected_sources
        )
        raise RuntimeError(f"V2 selected sources are not ready: {details}")
    run_view = await create_run_corpus_view(
        db,
        run,
        provider_index,
        usable_sources,
        usable_versions,
        canonical_provider_files,
        skipped_sources=skipped_sources,
        canonical_source_version_ids=canonical_version_ids,
        total_source_count=len(sources),
    )
    readiness = dict(run_view.readiness_summary)
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.CORPUS_PREPARE,
        agent_name=V2AgentName.SOURCE_CORPUS,
        tool_name=V2ToolName.PREPARE_V2_CORPUS,
        input_json={"source_count": len(sources)},
        output_json={
            "run_corpus_view_id": str(run_view.id),
            "corpus_revision": run_view.revision,
            "attempt": run_view.attempt,
            "provider_index_id": str(provider_index.id) if provider_index else None,
            "vector_store_id": provider_index.provider_vector_store_id if provider_index else None,
            "source_version_count": len(usable_versions),
            "provider_file_count": len(canonical_provider_files),
            "readiness_summary": readiness,
        },
    )
    return CorpusPreparationResult(
        run_view=run_view,
        provider_index=provider_index,
        sources=usable_sources,
        source_versions=usable_versions,
        provider_files=canonical_provider_files,
        vector_store_id=provider_index.provider_vector_store_id if provider_index else None,
        readiness_summary=readiness,
    )


_OPENAI_URL_READ_SCHEMA = OPENAI_URL_READ_SCHEMA
_OPENAI_URL_RECOVERY_SCHEMA = OPENAI_URL_RECOVERY_SCHEMA


_format_openai_url_read_document = format_openai_url_read_document
_loads_json_object = loads_json_object


async def list_evidence_dicts(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    run_view: RunCorpusView | None = None,
) -> list[dict[str, Any]]:
    from app.models.generation_v2 import EvidenceItem

    query = select(EvidenceItem).where(EvidenceItem.generation_run_id == run_id)
    scope = scope_for_run_view(run_view) if run_view is not None else session_corpus_scope_or_none(db, run_id)
    if scope is not None:
        query = query.where(
            EvidenceItem.corpus_revision == scope.revision,
            EvidenceItem.attempt == scope.attempt,
        )
    result = await db.execute(query.order_by(EvidenceItem.created_at.asc()))
    return [_evidence_to_dict(item) for item in result.scalars()]


async def get_evidence_dict(db: AsyncSession, run_id: uuid.UUID, evidence_id: uuid.UUID) -> dict[str, Any] | None:
    from app.models.generation_v2 import EvidenceItem

    result = await db.execute(
        select(EvidenceItem).where(EvidenceItem.generation_run_id == run_id, EvidenceItem.id == evidence_id)
    )
    item = result.scalar_one_or_none()
    return _evidence_to_dict(item) if item else None

__all__ = [
    "_agent_facing_provider_versions",
    "OpenAIUrlReadVersionDeps",
    "_call_openai_url_file_read",
    "_call_openai_url_recovery",
    "default_openai_url_read_version_deps",
    "_ensure_openai_url_read_versions",
    "_ensure_source_version",
    "_ensure_web_snapshot_versions",
    "_fetch_url_for_v2_snapshot",
    "_filename_from_url",
    "_is_pdf_response",
    "_provider_attributes",
    "_provider_uploads_required",
    "_skipped_source",
    "_canonical_provider_version",
    "_commit_if_supported",
    "SourceCorpusReconciliationDeps",
    "default_source_corpus_reconciliation_deps",
    "SourceCorpusProviderDetachDeps",
    "default_source_corpus_provider_detach_deps",
    "SourceCorpusProviderUploadDeps",
    "default_source_corpus_provider_upload_deps",
    "SourceCorpusProviderSyncDeps",
    "default_source_corpus_provider_sync_deps",
    "SourceCorpusProjectSyncDeps",
    "default_source_corpus_project_sync_deps",
    "ensure_project_provider_index",
    "ensure_source_versions",
    "download_file",
    "get_minio_client",
    "get_openai_client",
    "reconcile_project_corpus_index_now",
    "sync_provider_corpus_files",
    "sync_project_corpus_index_now",
    "sync_source_to_project_corpus_now",
    "_version_has_snapshot",
    "_version_upload_payload",
    "upload_file",
    "upload_text",
]
