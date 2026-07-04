from __future__ import annotations

import hashlib
import asyncio
import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import String, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.schemas import QuoteCard, SourceCard
from app.models.chunk import Chunk
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    CorpusWorkspace,
    SourceRepresentation,
    SourceRepresentationKind,
    V2RecordStatus,
)
from app.models.source import Source
from app.services.llm import get_openai_client
from app.services.storage import download_file, download_text, get_minio_client, upload_text

logger = logging.getLogger(__name__)


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

    source_cards: list[SourceCard] = []
    quote_cards: list[QuoteCard] = []
    ready_source_count = 0
    failed_source_count = 0
    ready_representation_count = 0
    failed_representation_count = 0
    workspace_warnings: list[str] = []
    for source in sources:
        representation_ids = await _register_source_representations(db, run, source, vector_store_id)
        ready_count, failed_count, coverage_status = await _representation_counts(db, representation_ids)
        ready_representation_count += ready_count
        failed_representation_count += failed_count
        if ready_count:
            ready_source_count += 1
        else:
            failed_source_count += 1
        chunks = await _load_source_chunks(db, source.id)
        extracted_preview = await _source_preview(source, chunks)
        cautions = _infer_cautions(source, extracted_preview)
        if not extracted_preview.strip() and ready_count:
            workspace_warnings.append(f"{source.title or source.id}: deterministic text missing; native/provider views available.")
        source_card = SourceCard(
            source_id=str(source.id),
            title=source.title or source.original_url or source.file_path or "Untitled source",
            source_type=source.source_type,
            url=source.original_url,
            reliability=source.domain_reliability or "unknown",
            authority=source.authority_type or "unknown",
            summary=_compact_preview(extracted_preview, 900),
            useful_for=_infer_useful_for(extracted_preview),
            gaps_or_cautions=cautions,
            representation_ids=[str(value) for value in representation_ids],
            representation_summary=f"{ready_count} ready, {failed_count} failed representations",
            ready_representation_count=ready_count,
            failed_representation_count=failed_count,
            coverage_status=coverage_status,
        )
        source_cards.append(source_card)
        quote_representation_id = await _ready_text_representation_id(db, source.id)
        quote_cards.extend(_quote_cards_from_chunks(source, chunks, representation_id=quote_representation_id))

    if ready_source_count == 0 or ready_representation_count == 0:
        workspace.status = V2RecordStatus.FAILED
        workspace.config = {
            **(workspace.config or {}),
            "workspace_warnings": workspace_warnings,
            "ready_source_count": ready_source_count,
            "failed_source_count": failed_source_count,
            "ready_representation_count": ready_representation_count,
            "failed_representation_count": failed_representation_count,
        }
        await db.flush()
        await save_tool_trace(
            db,
            generation_run_id=run.id,
            phase="v2_source_workspace",
            agent_name="source_workspace",
            tool_name="build_source_workspace",
            input_json={"source_count": len(sources)},
            output_json={
                "workspace_id": str(workspace.id),
                "status": "failed",
                "ready_source_count": ready_source_count,
                "failed_source_count": failed_source_count,
                "ready_representation_count": ready_representation_count,
                "failed_representation_count": failed_representation_count,
                "workspace_warnings": workspace_warnings,
            },
        )
        raise RuntimeError("V2 source workspace has no ready source representations")

    workspace.status = V2RecordStatus.READY
    workspace.config = {
        **(workspace.config or {}),
        "workspace_warnings": workspace_warnings,
        "ready_source_count": ready_source_count,
        "failed_source_count": failed_source_count,
        "ready_representation_count": ready_representation_count,
        "failed_representation_count": failed_representation_count,
    }
    await db.flush()
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase="v2_source_workspace",
        agent_name="source_workspace",
        tool_name="build_source_workspace",
        input_json={"source_count": len(sources)},
        output_json={
            "workspace_id": str(workspace.id),
            "source_cards": len(source_cards),
            "quote_cards": len(quote_cards),
            "vector_store_id": vector_store_id,
            "ready_source_count": ready_source_count,
            "failed_source_count": failed_source_count,
            "ready_representation_count": ready_representation_count,
            "failed_representation_count": failed_representation_count,
            "workspace_warnings": workspace_warnings,
        },
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


async def cleanup_provider_workspace(db: AsyncSession, run_id: uuid.UUID) -> None:
    if not settings.openai_api_key or not settings.generation_v2_cleanup_provider_files:
        return
    client = get_openai_client()
    workspace_result = await db.execute(
        select(CorpusWorkspace).where(CorpusWorkspace.generation_run_id == run_id, CorpusWorkspace.provider == "openai")
    )
    workspaces = list(workspace_result.scalars())
    vector_store_ids = [workspace.provider_vector_store_id for workspace in workspaces if workspace.provider_vector_store_id]
    rep_result = await db.execute(
        select(SourceRepresentation).where(
            SourceRepresentation.provider == "openai",
            SourceRepresentation.provider_file_id.is_not(None),
            SourceRepresentation.provider_vector_store_id.in_(vector_store_ids or [""]),
        )
    )
    provider_reps = list(rep_result.scalars())
    for rep in provider_reps:
        try:
            await client.files.delete(rep.provider_file_id)
            rep.metadata_ = {**(rep.metadata_ or {}), "provider_cleanup_status": "deleted"}
        except Exception:
            logger.info("OpenAI provider file cleanup failed for %s", rep.provider_file_id, exc_info=True)
    for workspace in workspaces:
        if not workspace.provider_vector_store_id:
            continue
        try:
            await client.vector_stores.delete(workspace.provider_vector_store_id)
            workspace.config = {**(workspace.config or {}), "provider_cleanup": "deleted"}
        except Exception:
            logger.info("OpenAI vector store cleanup failed for %s", workspace.provider_vector_store_id, exc_info=True)
    await db.flush()


async def _register_source_representations(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    vector_store_id: str | None,
) -> list[uuid.UUID]:
    representation_ids: list[uuid.UUID] = []

    if source.file_path:
        rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.ORIGINAL_FILE,
            storage_path=source.file_path,
            status=V2RecordStatus.READY,
            metadata={"canonical": True},
        )
        representation_ids.append(rep.id)
        await _maybe_upload_file_representation(db, run, source, rep, vector_store_id)

    if source.original_url:
        rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.ORIGINAL_URL,
            storage_path=source.original_url,
            status=V2RecordStatus.READY,
            metadata={"canonical": True},
        )
        representation_ids.append(rep.id)
        representation_ids.extend(await _capture_web_representations(db, source))

    if source.raw_text_path:
        try:
            text = download_text(get_minio_client(), source.raw_text_path)
            rep = await _save_representation(
                db,
                source=source,
                kind=(
                    SourceRepresentationKind.TRANSCRIPT
                    if source.source_type in {"audio", "video"}
                    else SourceRepresentationKind.EXTRACTED_TEXT
                ),
                storage_path=source.raw_text_path,
                status=V2RecordStatus.READY,
                content=text,
                metadata={"canonical": False, "view": "deterministic_ingestion"},
            )
            representation_ids.append(rep.id)
            await _maybe_upload_text_representation(db, run, source, rep, text, vector_store_id)
        except Exception as exc:
            logger.warning("Could not register extracted text for source %s", source.id, exc_info=True)
            rep = await _save_representation(
                db,
                source=source,
                kind=SourceRepresentationKind.EXTRACTED_TEXT,
                storage_path=source.raw_text_path,
                status=V2RecordStatus.FAILED,
                metadata={"canonical": False},
                error_message=str(exc),
            )
            representation_ids.append(rep.id)

    return representation_ids


async def _capture_web_representations(db: AsyncSession, source: Source) -> list[uuid.UUID]:
    if not source.original_url:
        return []

    reps: list[uuid.UUID] = []
    try:
        from app.ingestion.web import _safe_fetch

        response = await _safe_fetch(source.original_url)
        html = response.text
        object_name = f"v2/sources/{source.id}/fetched.html"
        upload_text(get_minio_client(), html, object_name)
        rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.FETCHED_HTML,
            storage_path=object_name,
            status=V2RecordStatus.READY,
            content=html,
            metadata={
                "url": str(response.url),
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "canonical": True,
            },
        )
        reps.append(rep.id)
    except Exception as exc:
        rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.FETCHED_HTML,
            storage_path=None,
            status=V2RecordStatus.FAILED,
            metadata={"url": source.original_url},
            error_message=str(exc),
        )
        reps.append(rep.id)

    if settings.generation_v2_enable_rendered_snapshots:
        reps.append((await _capture_rendered_snapshot(db, source)).id)

    return reps


async def _capture_rendered_snapshot(db: AsyncSession, source: Source) -> SourceRepresentation:
    if not source.original_url:
        return await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
            status=V2RecordStatus.FAILED,
            metadata={},
            error_message="Source has no URL",
        )
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.goto(source.original_url, wait_until="networkidle", timeout=settings.web_fetch_timeout * 1000)
            text = await page.locator("body").inner_text(timeout=5000)
            await browser.close()

        object_name = f"v2/sources/{source.id}/rendered.txt"
        upload_text(get_minio_client(), text, object_name)
        return await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
            storage_path=object_name,
            status=V2RecordStatus.READY,
            content=text,
            metadata={"url": source.original_url, "canonical": True, "format": "rendered_text"},
        )
    except Exception as exc:
        logger.info("Rendered snapshot unavailable for source %s: %s", source.id, exc)
        return await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
            status=V2RecordStatus.FAILED,
            metadata={"url": source.original_url},
            error_message=str(exc),
        )


async def _maybe_create_vector_store(
    db: AsyncSession,
    run: GenerationRun,
    workspace: CorpusWorkspace,
) -> str | None:
    if not settings.openai_api_key or not settings.generation_v2_enable_provider_uploads:
        return None
    try:
        client = get_openai_client()
        vector_store = await client.vector_stores.create(
            name=f"podgen-v2-{run.project_id}-{run.id}",
            metadata={"project_id": str(run.project_id), "generation_run_id": str(run.id)},
        )
        await save_tool_trace(
            db,
            generation_run_id=run.id,
            phase="v2_source_workspace",
            agent_name="source_workspace",
            tool_name="openai.vector_stores.create",
            input_json={"workspace_id": str(workspace.id)},
            output_json={"vector_store_id": vector_store.id},
            provider_call_id=vector_store.id,
        )
        return vector_store.id
    except Exception as exc:
        logger.warning("OpenAI vector store creation failed; continuing without hosted file search", exc_info=True)
        workspace.config = {**(workspace.config or {}), "provider_upload_error": str(exc)}
        return None


async def _maybe_upload_file_representation(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    rep: SourceRepresentation,
    vector_store_id: str | None,
) -> None:
    if not vector_store_id or not settings.openai_api_key or not source.file_path:
        return
    try:
        content = download_file(get_minio_client(), source.file_path)
        filename = (source.title or source.file_path.rsplit("/", 1)[-1] or f"{source.id}.bin").replace("/", "_")
        await _upload_provider_file(db, run, source, rep, filename, content, vector_store_id)
    except Exception as exc:
        logger.warning("Provider upload failed for source file %s", source.id, exc_info=True)
        rep.error_message = str(exc)


async def _maybe_upload_text_representation(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    rep: SourceRepresentation,
    text: str,
    vector_store_id: str | None,
) -> None:
    if not vector_store_id or not settings.openai_api_key or not text.strip():
        return
    filename = f"{source.id}-{rep.kind}.txt"
    try:
        await _upload_provider_file(db, run, source, rep, filename, text.encode("utf-8"), vector_store_id)
    except Exception as exc:
        logger.warning("Provider upload failed for source text %s", source.id, exc_info=True)
        rep.error_message = str(exc)


async def _upload_provider_file(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    rep: SourceRepresentation,
    filename: str,
    content: bytes,
    vector_store_id: str,
) -> None:
    existing_result = await db.execute(
        select(SourceRepresentation).where(
            SourceRepresentation.source_id == source.id,
            SourceRepresentation.kind == SourceRepresentationKind.PROVIDER_FILE,
            SourceRepresentation.provider == "openai",
            SourceRepresentation.provider_vector_store_id == vector_store_id,
        )
    )
    provider_rep = next(
        (
            item
            for item in existing_result.scalars()
            if str((item.metadata_ or {}).get("source_representation_id") or "") == str(rep.id)
        ),
        None,
    )
    if provider_rep is None:
        provider_rep = await _save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.PROVIDER_FILE,
            provider="openai",
            provider_vector_store_id=vector_store_id,
            status=V2RecordStatus.PENDING,
            content=content,
            metadata={
                "source_representation_id": str(rep.id),
                "filename": filename,
                "upload_phase": "db_prepared",
            },
        )
        await _commit_if_supported(db)
    client = get_openai_client()
    if not provider_rep.provider_file_id:
        file_obj = await asyncio.wait_for(
            client.files.create(file=(filename, io.BytesIO(content)), purpose="assistants"),
            timeout=max(30, int(settings.generation_v2_provider_upload_timeout_seconds)),
        )
        provider_rep.provider_file_id = file_obj.id
        provider_rep.status = V2RecordStatus.PENDING
        provider_rep.metadata_ = {
            **(provider_rep.metadata_ or {}),
            "upload_phase": "file_uploaded",
        }
        await db.flush()
        await _commit_if_supported(db)
    vector_file = await _workspace_retrieve_vector_store_file(client, vector_store_id, provider_rep.provider_file_id)
    if vector_file is None:
        vector_file = await asyncio.wait_for(
            client.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=provider_rep.provider_file_id,
                attributes={"source_id": str(source.id), "representation_id": str(rep.id), "kind": rep.kind},
            ),
            timeout=_workspace_provider_operation_timeout_seconds(),
        )
    provider_rep.metadata_ = {
        **(provider_rep.metadata_ or {}),
        "upload_phase": "attached",
        "provider_vector_store_file_id": getattr(vector_file, "id", None),
        "last_provider_status": getattr(vector_file, "status", None),
    }
    await db.flush()
    await _commit_if_supported(db)
    await _poll_workspace_provider_file(
        db,
        client,
        provider_rep,
        vector_store_id,
    )
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase="v2_source_workspace",
        agent_name="source_workspace",
        tool_name="openai.files.create+vector_store_file",
        input_json={"source_id": str(source.id), "representation_id": str(rep.id), "filename": filename},
        output_json={
            "provider_file_id": provider_rep.provider_file_id,
            "provider_vector_store_file_id": (provider_rep.metadata_ or {}).get("provider_vector_store_file_id"),
            "provider_vector_store_file_status": (provider_rep.metadata_ or {}).get("last_provider_status"),
            "provider_representation_id": str(provider_rep.id),
        },
        provider_call_id=provider_rep.provider_file_id,
    )


async def _poll_workspace_provider_file(
    db: AsyncSession,
    client: Any,
    provider_rep: SourceRepresentation,
    vector_store_id: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + max(30, int(settings.generation_v2_provider_upload_timeout_seconds))
    while True:
        vector_file = await _workspace_retrieve_vector_store_file(client, vector_store_id, provider_rep.provider_file_id)
        if vector_file is None:
            provider_rep.status = V2RecordStatus.PENDING
            provider_rep.metadata_ = {
                **(provider_rep.metadata_ or {}),
                "upload_phase": "indexing",
                "last_provider_status": "retrieve_unavailable",
            }
            await db.flush()
            await _commit_if_supported(db)
            return
        status = str(getattr(vector_file, "status", "") or "")
        provider_rep.metadata_ = {
            **(provider_rep.metadata_ or {}),
            "provider_vector_store_file_id": getattr(vector_file, "id", None),
            "last_provider_status": status,
        }
        if status == "completed":
            provider_rep.status = V2RecordStatus.READY
            provider_rep.error_message = None
            provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "ready"}
            await db.flush()
            await _commit_if_supported(db)
            return
        if status in {"failed", "cancelled"}:
            provider_rep.status = V2RecordStatus.FAILED
            provider_rep.error_message = f"OpenAI vector store file status: {status}"
            provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "failed"}
            await db.flush()
            await _commit_if_supported(db)
            return
        if asyncio.get_running_loop().time() >= deadline:
            provider_rep.status = V2RecordStatus.PENDING
            provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "indexing"}
            await db.flush()
            await _commit_if_supported(db)
            return
        provider_rep.status = V2RecordStatus.PENDING
        provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "indexing"}
        await db.flush()
        await _commit_if_supported(db)
        await asyncio.sleep(1)


async def _workspace_retrieve_vector_store_file(client: Any, vector_store_id: str, provider_file_id: str | None) -> Any | None:
    if not provider_file_id:
        return None
    try:
        return await asyncio.wait_for(
            client.vector_stores.files.retrieve(file_id=provider_file_id, vector_store_id=vector_store_id),
            timeout=_workspace_provider_operation_timeout_seconds(),
        )
    except Exception:
        return None


def _workspace_provider_operation_timeout_seconds() -> int:
    return max(10, min(60, int(settings.generation_v2_provider_upload_timeout_seconds)))


async def _commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()


async def _save_representation(
    db: AsyncSession,
    *,
    source: Source,
    kind: SourceRepresentationKind,
    storage_path: str | None = None,
    provider: str | None = None,
    provider_file_id: str | None = None,
    provider_vector_store_id: str | None = None,
    status: str = V2RecordStatus.PENDING,
    content: str | bytes | None = None,
    metadata: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> SourceRepresentation:
    content_hash = _hash_bytes(content) if content is not None else None
    rep = SourceRepresentation(
        source_id=source.id,
        kind=kind,
        storage_path=storage_path,
        provider=provider,
        provider_file_id=provider_file_id,
        provider_vector_store_id=provider_vector_store_id,
        content_hash=content_hash,
        status=status,
        metadata_=metadata or {},
        error_message=error_message,
    )
    db.add(rep)
    await db.flush()
    return rep


async def _load_source_chunks(db: AsyncSession, source_id: uuid.UUID) -> list[Chunk]:
    result = await db.execute(
        select(Chunk).where(Chunk.source_id == source_id).order_by(Chunk.chunk_index.asc()).limit(12)
    )
    return list(result.scalars())


async def _representation_counts(db: AsyncSession, representation_ids: list[uuid.UUID]) -> tuple[int, int, str]:
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


async def _ready_text_representation_id(db: AsyncSession, source_id: uuid.UUID) -> str | None:
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


async def _source_preview(source: Source, chunks: list[Chunk]) -> str:
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


def _source_set_hash(sources: list[Source]) -> str:
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


def _hash_bytes(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _compact_preview(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "No extracted preview is available; inspect the original source representation."
    return cleaned[:limit]


def _infer_useful_for(text: str) -> list[str]:
    lower = (text or "").lower()
    buckets: list[str] = []
    for label, markers in {
        "context": ("history", "timeline", "founded", "began", "started", "background"),
        "mechanism": ("mechanism", "because", "how it works", "process", "system"),
        "tension": ("conflict", "debate", "critic", "lawsuit", "controvers", "tradeoff"),
        "impact": ("impact", "result", "consequence", "cost", "effect"),
    }.items():
        if any(marker in lower for marker in markers):
            buckets.append(label)
    return buckets or ["background"]


def _infer_cautions(source: Source, text: str) -> list[str]:
    cautions: list[str] = []
    if not text.strip():
        cautions.append("No deterministic text view is available; rely on original/provider representation.")
    if source.domain_reliability in {"low", "unknown"}:
        cautions.append(f"Reliability is {source.domain_reliability}.")
    if source.source_type == "web" and not source.published_at:
        cautions.append("Published date is missing.")
    return cautions


def _quote_cards_from_chunks(source: Source, chunks: list[Chunk], *, representation_id: str | None = None) -> list[QuoteCard]:
    cards: list[QuoteCard] = []
    for chunk in chunks[:5]:
        quote = _best_quote(chunk.content)
        if not quote:
            continue
        cards.append(
            QuoteCard(
                source_id=str(source.id),
                quote=quote,
                locator=" > ".join(chunk.heading_path or []) or f"chunk {chunk.chunk_index}",
                narrative_use=", ".join(_infer_useful_for(chunk.content)),
                representation_id=representation_id,
                source_kind=SourceRepresentationKind.EXTRACTED_TEXT,
                confidence=0.65,
                confidence_reason="Selected from deterministic chunk text.",
            )
        )
    return cards


def _best_quote(text: str) -> str:
    paragraphs = [p.strip() for p in (text or "").split("\n") if len(p.strip()) >= 80]
    if not paragraphs:
        return ""
    return _compact_preview(paragraphs[0], 420)
