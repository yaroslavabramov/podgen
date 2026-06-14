from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import String, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    ProviderCorpusFile,
    ProviderCorpusIndex,
    ProviderCorpusScope,
    RunCorpusView,
    SourceVersion,
    SourceRepresentationKind,
    V2RecordStatus,
)
from app.models.source import Source
from app.services.llm import get_openai_client
from app.services.storage import download_file, download_text, get_minio_client, upload_text

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
    if not sources:
        raise RuntimeError("V2 corpus has no eligible project sources")

    all_versions: list[SourceVersion] = []
    for source in sources:
        all_versions.extend(await ensure_source_versions(db, source))
    active_versions = [version for version in all_versions if version.is_active and version.status == V2RecordStatus.READY]
    if not active_versions:
        raise RuntimeError("V2 corpus has no ready source versions")

    provider_index = await ensure_project_provider_index(db, run.project_id, run_id=run.id)
    provider_files = await sync_provider_corpus_files(db, run, provider_index, active_versions) if provider_index else []
    run_view = await create_run_corpus_view(db, run, provider_index, sources, active_versions, provider_files)
    readiness = dict(run_view.readiness_summary)
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase="v2_corpus_prepare",
        agent_name="source_corpus",
        tool_name="prepare_v2_corpus",
        input_json={"source_count": len(sources)},
        output_json={
            "run_corpus_view_id": str(run_view.id),
            "provider_index_id": str(provider_index.id) if provider_index else None,
            "vector_store_id": provider_index.provider_vector_store_id if provider_index else None,
            "source_version_count": len(active_versions),
            "provider_file_count": len(provider_files),
            "readiness_summary": readiness,
        },
    )
    return CorpusPreparationResult(
        run_view=run_view,
        provider_index=provider_index,
        sources=sources,
        source_versions=active_versions,
        provider_files=provider_files,
        vector_store_id=provider_index.provider_vector_store_id if provider_index else None,
        readiness_summary=readiness,
    )


async def ensure_source_versions(db: AsyncSession, source: Source) -> list[SourceVersion]:
    versions: list[SourceVersion] = []
    if source.file_path:
        file_hash = _hash_text(source.file_path)
        file_status = V2RecordStatus.READY
        try:
            file_hash = _hash_bytes(download_file(get_minio_client(), source.file_path))
        except Exception as exc:
            logger.info("Could not read original file for source version %s: %s", source.id, exc)
            file_status = V2RecordStatus.FAILED
        versions.append(
            await _ensure_source_version(
                db,
                source,
                kind=SourceRepresentationKind.ORIGINAL_FILE,
                storage_path=source.file_path,
                content_hash=file_hash,
                metadata={
                    "canonical": True,
                    "source_type": source.source_type,
                    "original_filename": source.title or Path(source.file_path).name,
                    "native_upload": True,
                },
                status=file_status,
            )
        )
    if source.original_url:
        versions.append(
            await _ensure_source_version(
                db,
                source,
                kind=SourceRepresentationKind.ORIGINAL_URL,
                storage_path=source.original_url,
                content_hash=_hash_text(source.original_url),
                metadata={"canonical": True, "url": source.original_url},
            )
        )
        versions.extend(await _ensure_web_snapshot_versions(db, source))
    if source.raw_text_path:
        text = ""
        status = V2RecordStatus.READY
        try:
            text = download_text(get_minio_client(), source.raw_text_path)
        except Exception as exc:
            logger.info("Could not read raw text for source version %s: %s", source.id, exc)
            status = V2RecordStatus.FAILED
        kind = SourceRepresentationKind.TRANSCRIPT if source.source_type in {"audio", "video"} else SourceRepresentationKind.EXTRACTED_TEXT
        versions.append(
            await _ensure_source_version(
                db,
                source,
                kind=kind,
                storage_path=source.raw_text_path,
                content_hash=_hash_text(text or source.raw_text_path),
                metadata={"canonical": False, "view": "deterministic_ingestion"},
                status=status,
            )
        )
        if text.strip():
            page_marked_text = _page_mark_text(text, source)
            page_marked_hash = _hash_text(page_marked_text)
            page_marked_path = _versioned_object_name(source.id, page_marked_hash, "page_marked.txt")
            upload_text(get_minio_client(), page_marked_text, page_marked_path)
            versions.append(
                await _ensure_source_version(
                    db,
                    source,
                    kind=SourceRepresentationKind.PAGE_MARKED_TEXT,
                    storage_path=page_marked_path,
                    content_hash=page_marked_hash,
                    metadata={"canonical": False, "view": "page_marked_companion"},
                )
            )
    await db.flush()
    return versions


async def ensure_project_provider_index(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    run_id: uuid.UUID | None = None,
) -> ProviderCorpusIndex | None:
    result = await db.execute(
        select(ProviderCorpusIndex).where(
            ProviderCorpusIndex.project_id == project_id,
            ProviderCorpusIndex.provider == "openai",
            ProviderCorpusIndex.scope == ProviderCorpusScope.PROJECT,
        )
    )
    existing = result.scalar_one_or_none()
    if existing and existing.provider_vector_store_id and settings.openai_api_key and settings.generation_v2_enable_provider_uploads:
        if existing.status != V2RecordStatus.READY:
            existing.status = V2RecordStatus.READY
            existing.error_message = None
            existing.config = {
                **(existing.config or {}),
                "provider_uploads_enabled": True,
            }
            await db.flush()
        return existing
    if not settings.openai_api_key or not settings.generation_v2_enable_provider_uploads:
        error_message = (
            "OPENAI_API_KEY is not configured; provider index unavailable."
            if not settings.openai_api_key
            else "Provider corpus uploads are disabled; provider index unavailable."
        )
        if existing is None:
            existing = ProviderCorpusIndex(
                project_id=project_id,
                provider="openai",
                scope=ProviderCorpusScope.PROJECT,
                status=V2RecordStatus.FAILED,
                config={"provider_uploads_enabled": bool(settings.generation_v2_enable_provider_uploads)},
                error_message=error_message,
            )
            db.add(existing)
        else:
            existing.status = V2RecordStatus.FAILED
            existing.error_message = error_message
            existing.config = {
                **(existing.config or {}),
                "provider_uploads_enabled": bool(settings.generation_v2_enable_provider_uploads),
            }
        await db.flush()
        return existing

    client = get_openai_client()
    vector_store = await client.vector_stores.create(
        name=f"podgen-v2-project-{project_id}",
        metadata={"project_id": str(project_id), "scope": "project"},
    )
    if existing is None:
        existing = ProviderCorpusIndex(
            project_id=project_id,
            provider="openai",
            provider_vector_store_id=vector_store.id,
            scope=ProviderCorpusScope.PROJECT,
            status=V2RecordStatus.READY,
            config={"created_by_run_id": str(run_id) if run_id else None},
        )
        db.add(existing)
    else:
        existing.provider_vector_store_id = vector_store.id
        existing.status = V2RecordStatus.READY
        existing.error_message = None
        existing.config = {**(existing.config or {}), "created_by_run_id": str(run_id) if run_id else None}
    await db.flush()
    return existing


async def sync_provider_corpus_files(
    db: AsyncSession,
    run: GenerationRun | None,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
) -> list[ProviderCorpusFile]:
    if not provider_index.provider_vector_store_id or not settings.openai_api_key:
        return []
    synced: list[ProviderCorpusFile] = []
    for version in source_versions:
        if not _version_uploadable(version):
            continue
        synced.append(await _ensure_provider_file(db, run, provider_index, version))
    await db.flush()
    return synced


async def sync_project_corpus_index_now(db: AsyncSession, project_id: uuid.UUID) -> None:
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
    sources = list(source_result.scalars())
    versions: list[SourceVersion] = []
    for source in sources:
        versions.extend(await ensure_source_versions(db, source))
    active_versions = [version for version in versions if version.is_active and version.status == V2RecordStatus.READY]
    provider_index = await ensure_project_provider_index(db, project_id)
    if provider_index:
        await sync_provider_corpus_files(db, None, provider_index, active_versions)
    await db.flush()


async def create_run_corpus_view(
    db: AsyncSession,
    run: GenerationRun,
    provider_index: ProviderCorpusIndex | None,
    sources: list[Source],
    source_versions: list[SourceVersion],
    provider_files: list[ProviderCorpusFile],
) -> RunCorpusView:
    existing_result = await db.execute(select(RunCorpusView).where(RunCorpusView.generation_run_id == run.id))
    existing = existing_result.scalar_one_or_none()
    selected_source_ids = [str(source.id) for source in sources]
    selected_version_ids = [str(version.id) for version in source_versions]
    eligible_provider_file_ids = [file.provider_file_id for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY]
    provider_ready_source_ids = {str(file.source_id) for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY}
    local_snapshot_source_ids = {str(version.source_id) for version in source_versions if _version_has_snapshot(version)}
    usable_source_ids = provider_ready_source_ids | local_snapshot_source_ids
    readiness = {
        "source_count": len(sources),
        "source_version_count": len(source_versions),
        "provider_file_count": len(eligible_provider_file_ids),
        "provider_index_ready": bool(provider_index and provider_index.status == V2RecordStatus.READY),
        "snapshot_backed_source_count": len(local_snapshot_source_ids),
        "provider_ready_source_count": len(provider_ready_source_ids),
        "usable_source_count": len(usable_source_ids),
    }
    if not readiness["source_version_count"] or not readiness["usable_source_count"]:
        status = V2RecordStatus.FAILED
    else:
        status = V2RecordStatus.READY
    manifest_payload = {
        "sources": selected_source_ids,
        "versions": selected_version_ids,
        "provider_files": eligible_provider_file_ids,
        "provider_index_id": str(provider_index.id) if provider_index else None,
    }
    manifest_hash = hashlib.sha256(json.dumps(manifest_payload, sort_keys=True).encode("utf-8")).hexdigest()
    values = {
        "project_id": run.project_id,
        "provider_index_id": provider_index.id if provider_index else None,
        "selected_source_ids": selected_source_ids,
        "selected_source_version_ids": selected_version_ids,
        "active_source_filter": {"source_ids": selected_source_ids, "source_version_ids": selected_version_ids},
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
            raise RuntimeError("V2 run corpus view has no eligible source versions")
        return existing
    run_view = RunCorpusView(generation_run_id=run.id, **values)
    db.add(run_view)
    await db.flush()
    if run_view.status == V2RecordStatus.FAILED:
        raise RuntimeError("V2 run corpus view has no eligible source versions")
    return run_view


async def list_evidence_dicts(db: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    from app.models.generation_v2 import EvidenceItem

    result = await db.execute(
        select(EvidenceItem).where(EvidenceItem.generation_run_id == run_id).order_by(EvidenceItem.created_at.asc())
    )
    return [_evidence_to_dict(item) for item in result.scalars()]


async def get_evidence_dict(db: AsyncSession, run_id: uuid.UUID, evidence_id: uuid.UUID) -> dict[str, Any] | None:
    from app.models.generation_v2 import EvidenceItem

    result = await db.execute(
        select(EvidenceItem).where(EvidenceItem.generation_run_id == run_id, EvidenceItem.id == evidence_id)
    )
    item = result.scalar_one_or_none()
    return _evidence_to_dict(item) if item else None


async def project_corpus_index_summary(db: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    index_result = await db.execute(
        select(ProviderCorpusIndex).where(
            ProviderCorpusIndex.project_id == project_id,
            ProviderCorpusIndex.provider == "openai",
            ProviderCorpusIndex.scope == ProviderCorpusScope.PROJECT,
        )
    )
    index = index_result.scalar_one_or_none()
    version_result = await db.execute(select(SourceVersion).where(SourceVersion.project_id == project_id))
    versions = list(version_result.scalars())
    files: list[ProviderCorpusFile] = []
    if index:
        file_result = await db.execute(select(ProviderCorpusFile).where(ProviderCorpusFile.provider_index_id == index.id))
        files = list(file_result.scalars())
    return {
        "project_id": str(project_id),
        "provider_index": {
            "id": str(index.id) if index else None,
            "provider": index.provider if index else "openai",
            "provider_vector_store_id": index.provider_vector_store_id if index else None,
            "status": index.status if index else "missing",
            "error_message": index.error_message if index else None,
        },
        "source_version_count": len(versions),
        "active_source_version_count": len([version for version in versions if version.is_active]),
        "provider_file_count": len(files),
        "ready_provider_file_count": len([file for file in files if file.status == V2RecordStatus.READY]),
    }


async def _load_run_sources(db: AsyncSession, run: GenerationRun) -> list[Source]:
    attached_source_ids = set(str(value) for value in (run.config or {}).get("source_ids", []) if value)
    query = (
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
    result = await db.execute(query)
    return list(result.scalars())


async def _ensure_web_snapshot_versions(db: AsyncSession, source: Source) -> list[SourceVersion]:
    versions: list[SourceVersion] = []
    if not source.original_url:
        return versions
    try:
        from app.ingestion.web import _safe_fetch

        response = await _safe_fetch(source.original_url)
        html = response.text
        content_hash = _hash_text(html)
        object_name = _versioned_object_name(source.id, content_hash, "fetched.html")
        upload_text(get_minio_client(), html, object_name)
        versions.append(
            await _ensure_source_version(
                db,
                source,
                kind=SourceRepresentationKind.FETCHED_HTML,
                storage_path=object_name,
                content_hash=content_hash,
                metadata={
                    "canonical": True,
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                },
            )
        )
    except Exception as exc:
        logger.info("Fetched HTML source version unavailable for %s: %s", source.id, exc)
    if settings.generation_v2_enable_rendered_snapshots:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page()
                await page.goto(source.original_url, wait_until="networkidle", timeout=settings.web_fetch_timeout * 1000)
                text = await page.locator("body").inner_text(timeout=5000)
                await browser.close()
            content_hash = _hash_text(text)
            object_name = _versioned_object_name(source.id, content_hash, "rendered.txt")
            upload_text(get_minio_client(), text, object_name)
            versions.append(
                await _ensure_source_version(
                    db,
                    source,
                    kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
                    storage_path=object_name,
                    content_hash=content_hash,
                    metadata={"canonical": True, "url": source.original_url, "format": "rendered_text"},
                )
            )
        except Exception as exc:
            logger.info("Rendered source version unavailable for %s: %s", source.id, exc)
    return versions


async def _ensure_source_version(
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
    if previous:
        previous.is_active = False
        await mark_provider_files_inactive(
            db,
            source_version_id=previous.id,
            cleanup_status="retained_for_historical_evidence",
        )
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
    return version


async def mark_provider_files_inactive(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None = None,
    source_version_id: uuid.UUID | None = None,
    cleanup_status: str = "retained_for_historical_evidence",
) -> None:
    if source_id is None and source_version_id is None:
        return
    conditions = []
    if source_id is not None:
        conditions.append(ProviderCorpusFile.source_id == source_id)
    if source_version_id is not None:
        conditions.append(ProviderCorpusFile.source_version_id == source_version_id)
    result = await db.execute(select(ProviderCorpusFile).where(*conditions))
    for provider_file in result.scalars():
        provider_file.attributes = {**(provider_file.attributes or {}), "is_active": False}
        provider_file.cleanup_status = cleanup_status
        await _detach_provider_file_from_active_index(db, provider_file)
    await db.flush()


async def _detach_provider_file_from_active_index(db: AsyncSession, provider_file: ProviderCorpusFile) -> None:
    if not settings.openai_api_key or not provider_file.provider_file_id:
        return
    provider_index = await db.get(ProviderCorpusIndex, provider_file.provider_index_id)
    if not provider_index or not provider_index.provider_vector_store_id:
        return
    try:
        client = get_openai_client()
        await client.vector_stores.files.delete(
            vector_store_id=provider_index.provider_vector_store_id,
            file_id=provider_file.provider_file_id,
        )
        provider_file.cleanup_status = "removed_from_active_index_retained_for_historical_evidence"
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "active_index_status": "removed",
        }
    except Exception as exc:
        logger.info("Could not detach provider corpus file %s from active index: %s", provider_file.id, exc)
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "active_index_status": "detach_failed",
        }


async def _ensure_provider_file(
    db: AsyncSession,
    run: GenerationRun | None,
    provider_index: ProviderCorpusIndex,
    version: SourceVersion,
) -> ProviderCorpusFile:
    existing_result = await db.execute(
        select(ProviderCorpusFile).where(
            ProviderCorpusFile.provider_index_id == provider_index.id,
            ProviderCorpusFile.source_version_id == version.id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing and existing.status == V2RecordStatus.READY:
        return existing
    attributes = _provider_attributes(version)
    if existing is None:
        existing = ProviderCorpusFile(
            provider_index_id=provider_index.id,
            source_version_id=version.id,
            source_id=version.source_id,
            provider="openai",
            attributes=attributes,
            status=V2RecordStatus.PENDING,
        )
        db.add(existing)
        await db.flush()
    try:
        filename, content = _version_upload_payload(version)
        if not content:
            raise RuntimeError("Source version content is empty")
        client = get_openai_client()
        file_obj = await client.files.create(file=(filename, io.BytesIO(content)), purpose="assistants")
        vector_file = await client.vector_stores.files.create_and_poll(
            vector_store_id=provider_index.provider_vector_store_id,
            file_id=file_obj.id,
            attributes=attributes,
        )
        existing.provider_file_id = file_obj.id
        existing.provider_vector_store_file_id = getattr(vector_file, "id", None)
        existing.status = V2RecordStatus.READY
        existing.error_message = None
        version.provider = "openai"
        version.provider_file_id = file_obj.id
        version.provider_vector_store_file_id = getattr(vector_file, "id", None)
        version.upload_status = V2RecordStatus.READY
        if run is not None:
            await save_tool_trace(
                db,
                generation_run_id=run.id,
                phase="v2_corpus_prepare",
                agent_name="source_corpus",
                tool_name="openai.files.create+vector_store_file",
                input_json={"source_version_id": str(version.id), "filename": filename, "attributes": attributes},
                output_json={
                    "provider_file_id": file_obj.id,
                    "provider_vector_store_file_id": getattr(vector_file, "id", None),
                    "status": getattr(vector_file, "status", None),
                },
                provider_call_id=file_obj.id,
            )
    except Exception as exc:
        logger.info("Provider corpus file sync failed for %s: %s", version.id, exc, exc_info=True)
        existing.status = V2RecordStatus.FAILED
        existing.error_message = str(exc)
        version.upload_status = V2RecordStatus.FAILED
    await db.flush()
    return existing


def _version_uploadable(version: SourceVersion) -> bool:
    return bool(version.storage_path and version.kind != SourceRepresentationKind.ORIGINAL_URL and version.status == V2RecordStatus.READY)


def _version_has_snapshot(version: SourceVersion) -> bool:
    return bool(
        version.storage_path
        and version.status == V2RecordStatus.READY
        and version.kind
        in {
            SourceRepresentationKind.FETCHED_HTML,
            SourceRepresentationKind.RENDERED_SNAPSHOT,
            SourceRepresentationKind.EXTRACTED_TEXT,
            SourceRepresentationKind.PAGE_MARKED_TEXT,
            SourceRepresentationKind.TRANSCRIPT,
        }
    )


def _version_upload_payload(version: SourceVersion) -> tuple[str, bytes]:
    if not version.storage_path:
        return _provider_filename(version, ".txt"), b""
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE:
        return _provider_filename(version), download_file(get_minio_client(), version.storage_path)
    text = download_text(get_minio_client(), version.storage_path)
    return _provider_filename(version, ".txt"), text.encode("utf-8")


def _provider_filename(version: SourceVersion, suffix: str | None = None) -> str:
    metadata = version.metadata_ or {}
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE:
        raw_name = str(metadata.get("original_filename") or f"{version.source_id}-{version.kind}")
        filename = Path(raw_name).name or f"{version.source_id}-{version.kind}"
        if "." not in filename and metadata.get("source_type") == "pdf":
            filename = f"{filename}.pdf"
    else:
        filename = f"{version.source_id}-{version.kind}-{version.id}{suffix or ''}"
    return filename.replace("/", "_")


def _provider_attributes(version: SourceVersion) -> dict[str, str | bool | float]:
    metadata = version.metadata_ or {}
    return {
        "project_id": str(version.project_id),
        "source_id": str(version.source_id),
        "source_version_id": str(version.id),
        "source_type": str(metadata.get("source_type") or "unknown"),
        "representation_kind": version.kind,
        "content_hash": version.content_hash or "",
        "authority": str(metadata.get("authority") or "unknown"),
        "is_active": bool(version.is_active),
        "scope": "project",
    }


def _page_mark_text(text: str, source: Source) -> str:
    paragraphs = [paragraph.strip() for paragraph in text.split("\n") if paragraph.strip()]
    lines = [f"[source_id={source.id} page_marker=1 paragraph={idx + 1}] {paragraph}" for idx, paragraph in enumerate(paragraphs)]
    return "\n\n".join(lines)


def _evidence_to_dict(item) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "generation_run_id": str(item.generation_run_id),
        "project_id": str(item.project_id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "provider": item.provider,
        "provider_vector_store_id": item.provider_vector_store_id,
        "provider_file_id": item.provider_file_id,
        "provider_vector_store_file_id": item.provider_vector_store_file_id,
        "response_id": item.response_id,
        "tool_call_id": item.tool_call_id,
        "retrieval_channel": item.retrieval_channel,
        "query_text": item.query_text,
        "purpose": item.purpose,
        "search_result_index": item.search_result_index,
        "score": item.score,
        "snippet_text": item.snippet_text,
        "normalized_quote_text": item.normalized_quote_text,
        "locator_text": item.locator_text,
        "locator_type": item.locator_type,
        "evidence_strength": item.evidence_strength,
        "snippet_hash": item.snippet_hash,
        "metadata": item.metadata_,
        "created_at": item.created_at.isoformat() if isinstance(item.created_at, datetime) else None,
    }


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _versioned_object_name(source_id: uuid.UUID, content_hash: str, filename: str) -> str:
    return f"v2/source_versions/{source_id}/{content_hash}/{filename}"
