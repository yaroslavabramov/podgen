from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.schemas import QuoteCard, SourceCard
from app.generation_v2.source_workspace_build import build_source_workspace_cards as _build_source_workspace_cards
from app.generation_v2.source_workspace_cards import (
    compact_preview as _compact_preview,
    infer_cautions as _infer_cautions,
    infer_useful_for as _infer_useful_for,
    quote_cards_from_chunks as _quote_cards_from_chunks,
)
from app.generation_v2.source_workspace_helpers import (
    load_source_chunks as _load_source_chunks,
    ready_text_representation_id as _ready_text_representation_id,
    representation_counts as _representation_counts,
    source_preview as _source_preview,
    source_set_hash as _source_set_hash,
)
from app.generation_v2.source_workspace_finalize import (
    fail_source_workspace,
    mark_source_workspace_ready,
)
from app.generation_v2.source_workspace_representations import (
    capture_rendered_snapshot as _capture_rendered_snapshot,
    capture_web_representations as _capture_web_representations,
    hash_bytes as _hash_bytes,
    register_source_representations as _register_source_representations,
    save_representation as _save_representation,
)

from app.generation_v2.source_workspace_provider import (
    cleanup_provider_workspace,
    _commit_if_supported,
    _maybe_create_vector_store,
    _maybe_upload_file_representation,
    _maybe_upload_text_representation,
    _poll_workspace_provider_file,
    _upload_provider_file,
    _workspace_provider_operation_timeout_seconds,
    _workspace_retrieve_vector_store_file,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    CorpusWorkspace,
    V2RecordStatus,
)
from app.models.source import Source
from app.services.llm import get_openai_client
from app.services.storage import download_file


@dataclass
class WorkspaceBuildResult:
    workspace: CorpusWorkspace
    source_cards: list[SourceCard]
    quote_cards: list[QuoteCard]
    vector_store_id: str | None
    ready_source_count: int
    failed_source_count: int
    ready_representation_count: int
    failed_representation_count: int
    workspace_warnings: list[str]


async def build_source_workspace(db: AsyncSession, run: GenerationRun) -> WorkspaceBuildResult:
    """Create the V2 source workspace views for a run.

    Original files/URLs are represented directly. Extracted text and chunks become
    searchable support views, but the workspace keeps originals/snapshots visible.
    """
    attached_source_ids = set(str(value) for value in (run.config or {}).get("source_ids", []) if value)
    result = await db.execute(
        select(Source)
        .where(
            Source.project_id == run.project_id,
            or_(Source.is_temporary.is_(False), Source.id.cast(String).in_(attached_source_ids)),
            or_(
                Source.ingestion_status == "completed",
                Source.file_path.is_not(None),
                Source.original_url.is_not(None),
                Source.raw_text_path.is_not(None),
            ),
        )
        .order_by(Source.created_at.asc())
    )
    sources = list(result.scalars())
    source_set_hash = _source_set_hash(sources)

    workspace = CorpusWorkspace(
        project_id=run.project_id,
        generation_run_id=run.id,
        provider="openai" if settings.openai_api_key else None,
        source_set_hash=source_set_hash,
        status=V2RecordStatus.PENDING,
        config={
            "native_first": True,
            "deterministic_views_are_supporting": True,
            "provider_uploads_enabled": bool(settings.generation_v2_enable_provider_uploads),
            "rendered_snapshots_enabled": bool(settings.generation_v2_enable_rendered_snapshots),
        },
    )
    db.add(workspace)
    await db.flush()

    vector_store_id = await _maybe_create_vector_store(db, run, workspace)
    workspace.provider_vector_store_id = vector_store_id

    cards_result = await _build_source_workspace_cards(db, run, sources=sources, vector_store_id=vector_store_id)
    source_cards = cards_result.source_cards
    quote_cards = cards_result.quote_cards
    ready_source_count = cards_result.ready_source_count
    failed_source_count = cards_result.failed_source_count
    ready_representation_count = cards_result.ready_representation_count
    failed_representation_count = cards_result.failed_representation_count
    workspace_warnings = cards_result.workspace_warnings

    if ready_source_count == 0 or ready_representation_count == 0:
        await fail_source_workspace(
            db,
            run=run,
            workspace=workspace,
            source_count=len(sources),
            ready_source_count=ready_source_count,
            failed_source_count=failed_source_count,
            ready_representation_count=ready_representation_count,
            failed_representation_count=failed_representation_count,
            workspace_warnings=workspace_warnings,
        )
        raise RuntimeError("V2 source workspace has no ready source representations")

    await mark_source_workspace_ready(
        db,
        run=run,
        workspace=workspace,
        source_count=len(sources),
        source_card_count=len(source_cards),
        quote_card_count=len(quote_cards),
        vector_store_id=vector_store_id,
        ready_source_count=ready_source_count,
        failed_source_count=failed_source_count,
        ready_representation_count=ready_representation_count,
        failed_representation_count=failed_representation_count,
        workspace_warnings=workspace_warnings,
    )
    return WorkspaceBuildResult(
        workspace,
        source_cards,
        quote_cards[:80],
        vector_store_id,
        ready_source_count,
        failed_source_count,
        ready_representation_count,
        failed_representation_count,
        workspace_warnings,
    )

__all__ = [
    "cleanup_provider_workspace",
    "_commit_if_supported",
    "_capture_rendered_snapshot",
    "_capture_web_representations",
    "_hash_bytes",
    "_maybe_upload_file_representation",
    "_maybe_upload_text_representation",
    "_poll_workspace_provider_file",
    "_save_representation",
    "_upload_provider_file",
    "_workspace_provider_operation_timeout_seconds",
    "_workspace_retrieve_vector_store_file",
    "get_openai_client",
    "download_file",
    "_build_source_workspace_cards",
    "_compact_preview",
    "_infer_cautions",
    "_infer_useful_for",
    "_load_source_chunks",
    "_quote_cards_from_chunks",
    "_ready_text_representation_id",
    "_representation_counts",
    "_register_source_representations",
    "_source_preview",
    "_source_set_hash",
    "fail_source_workspace",
    "mark_source_workspace_ready",
    "save_tool_trace",
]
