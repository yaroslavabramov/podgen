from __future__ import annotations

import hashlib
import asyncio
import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import String, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.models.generation_run import GenerationRun
from app.models.project import Project
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
from app.services.storage import download_file, download_text, get_minio_client, upload_file, upload_text

logger = logging.getLogger(__name__)


_PROVIDER_PHASE_DB_PREPARED = "db_prepared"
_PROVIDER_PHASE_FILE_UPLOADED = "file_uploaded"
_PROVIDER_PHASE_ATTACHED = "attached"
_PROVIDER_PHASE_INDEXING = "indexing"
_PROVIDER_PHASE_READY = "ready"
_PROVIDER_PHASE_FAILED = "failed"
_PROVIDER_PHASE_ADOPTED = "adopted"
_PROVIDER_PHASE_DETACHED = "detached"
_PROVIDER_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_PROVIDER_INTERNAL_ATTRIBUTE_KEYS = {
    "upload_phase",
    "last_provider_status",
    "last_reconciled_at",
    "adoption_source",
    "last_openai_error",
    "active_index_status",
    "excluded_reason",
}


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

    provider_index = await _load_project_provider_index(db, run.project_id)
    all_versions = await _load_active_ready_source_versions(db, run.project_id, [source.id for source in sources])
    provider_files = await _load_ready_provider_files(db, provider_index, [version.id for version in all_versions])
    usable_sources, usable_versions, canonical_provider_files, skipped_sources, canonical_version_ids = _select_ready_run_corpus(
        sources,
        all_versions,
        provider_files,
        provider_index,
    )
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
        phase="v2_corpus_prepare",
        agent_name="source_corpus",
        tool_name="prepare_v2_corpus",
        input_json={"source_count": len(sources)},
        output_json={
            "run_corpus_view_id": str(run_view.id),
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


async def _load_project_provider_index(db: AsyncSession, project_id: uuid.UUID) -> ProviderCorpusIndex | None:
    result = await db.execute(
        select(ProviderCorpusIndex).where(
            ProviderCorpusIndex.project_id == project_id,
            ProviderCorpusIndex.provider == "openai",
            ProviderCorpusIndex.scope == ProviderCorpusScope.PROJECT,
        )
    )
    return result.scalar_one_or_none()


async def _load_active_ready_source_versions(
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
            SourceVersion.is_active.is_(True),
            SourceVersion.status == V2RecordStatus.READY,
        )
        .order_by(SourceVersion.created_at.asc())
    )
    return list(result.scalars())


async def _load_ready_source_versions_for_sources(
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


async def _load_ready_provider_files(
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


def _select_ready_run_corpus(
    sources: list[Source],
    source_versions: list[SourceVersion],
    provider_files: list[ProviderCorpusFile],
    provider_index: ProviderCorpusIndex | None,
) -> tuple[list[Source], list[SourceVersion], list[ProviderCorpusFile], list[dict[str, Any]], list[str]]:
    versions_by_source = _versions_by_source(source_versions)
    provider_by_version = {file.source_version_id: file for file in provider_files if _provider_file_search_ready(file)}
    provider_required = _provider_uploads_required()
    provider_index_ready = bool(provider_index and provider_index.provider_vector_store_id and provider_index.status == V2RecordStatus.READY)
    usable_sources: list[Source] = []
    usable_versions: list[SourceVersion] = []
    canonical_provider_files: list[ProviderCorpusFile] = []
    canonical_version_ids: list[str] = []
    skipped_sources: list[dict[str, Any]] = []
    for source in sources:
        versions = versions_by_source.get(source.id, [])
        if not versions:
            skipped_sources.append(_skipped_source(source, "no_ready_source_versions"))
            continue
        canonical = _canonical_provider_version(versions)
        if canonical is None:
            skipped_sources.append(_skipped_source(source, "no_canonical_source_version"))
            continue
        provider_file = provider_by_version.get(canonical.id)
        if provider_required:
            if not provider_index_ready:
                skipped_sources.append(
                    _skipped_source(source, "provider_index_not_ready", canonical_version_id=str(canonical.id))
                )
                continue
            if provider_file is None:
                skipped_sources.append(
                    _skipped_source(source, "canonical_provider_file_not_ready", canonical_version_id=str(canonical.id))
                )
                continue
            canonical_provider_files.append(provider_file)
        elif provider_file is not None:
            canonical_provider_files.append(provider_file)
        usable_sources.append(source)
        usable_versions.extend(versions)
        canonical_version_ids.append(str(canonical.id))
    return usable_sources, usable_versions, canonical_provider_files, skipped_sources, canonical_version_ids


def _skipped_source(source: Source, reason: str, *, canonical_version_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_id": str(source.id),
        "reason": reason,
        "source_type": str(source.source_type),
    }
    if canonical_version_id:
        payload["canonical_version_id"] = canonical_version_id
    return payload


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
        versions.extend(await _ensure_openai_url_read_versions(db, source))
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


_OPENAI_URL_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "read_status": {
            "type": "string",
            "enum": ["readable", "partial", "blocked", "not_found", "unsupported", "error"],
        },
        "title": {"type": "string"},
        "canonical_url": {"type": "string"},
        "content_type": {"type": "string"},
        "extracted_text_markdown": {"type": "string"},
        "summary": {"type": "string"},
        "failure_reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": [
        "read_status",
        "title",
        "canonical_url",
        "content_type",
        "extracted_text_markdown",
        "summary",
        "failure_reason",
        "confidence",
    ],
}


_OPENAI_URL_RECOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "recovery_status": {
            "type": "string",
            "enum": ["same_url_readable", "alternate_found", "paywalled_or_blocked", "gone", "not_found", "unclear"],
        },
        "original_url_status": {"type": "string"},
        "best_alternative_url": {"type": "string"},
        "alternative_urls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["url", "title", "reason", "confidence"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["recovery_status", "original_url_status", "best_alternative_url", "alternative_urls", "notes"],
}


async def _ensure_openai_url_read_versions(db: AsyncSession, source: Source) -> list[SourceVersion]:
    versions: list[SourceVersion] = []
    if not source.original_url or not settings.generation_v2_enable_openai_url_read or not settings.openai_api_key:
        return versions
    existing_result = await db.execute(
        select(SourceVersion).where(
            SourceVersion.source_id == source.id,
            SourceVersion.kind == SourceRepresentationKind.MODEL_READ_TEXT,
            SourceVersion.is_active.is_(True),
            SourceVersion.status == V2RecordStatus.READY,
        )
    )
    existing_versions = list(existing_result.scalars())
    if existing_versions:
        return existing_versions

    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
    prior_read = metadata.get("v2_openai_url_read")
    if isinstance(prior_read, dict) and prior_read.get("synced_at"):
        return versions

    read_result: dict[str, Any] | None = None
    try:
        read_result = await asyncio.wait_for(
            _call_openai_url_file_read(source.original_url),
            timeout=max(1, settings.generation_v2_openai_url_read_timeout_seconds),
        )
    except Exception as exc:
        source.metadata_ = {
            **(source.metadata_ or {}),
            "v2_openai_url_read": {
                "status": "error",
                "error": str(exc),
                "synced_at": datetime.utcnow().isoformat(),
            },
        }

    if read_result:
        text = str(read_result.get("extracted_text_markdown") or "").strip()
        read_status = str(read_result.get("read_status") or "error")
        source.metadata_ = {
            **(source.metadata_ or {}),
            "v2_openai_url_read": {
                "status": read_status,
                "title": str(read_result.get("title") or ""),
                "canonical_url": str(read_result.get("canonical_url") or source.original_url),
                "content_type": str(read_result.get("content_type") or ""),
                "summary": str(read_result.get("summary") or ""),
                "failure_reason": str(read_result.get("failure_reason") or ""),
                "confidence": float(read_result.get("confidence") or 0),
                "response_id": str(read_result.get("_response_id") or ""),
                "synced_at": datetime.utcnow().isoformat(),
            },
        }
        if not source.title and read_result.get("title"):
            source.title = str(read_result["title"])[:500]
        if text and not source.text_preview:
            source.text_preview = text[:500]
        if len(text) >= settings.generation_v2_openai_url_read_min_chars and read_status in {"readable", "partial"}:
            document = _format_openai_url_read_document(source.original_url, read_result, text)
            content_hash = _hash_text(document)
            object_name = _versioned_object_name(source.id, content_hash, "openai_url_read.md")
            upload_text(get_minio_client(), document, object_name)
            versions.append(
                await _ensure_source_version(
                    db,
                    source,
                    kind=SourceRepresentationKind.MODEL_READ_TEXT,
                    storage_path=object_name,
                    content_hash=content_hash,
                    metadata={
                        "canonical": False,
                        "view": "openai_url_read",
                        "source_type": "web",
                        "native_upload": True,
                        "native_openai_url_read": True,
                        "url": source.original_url,
                        "canonical_url": str(read_result.get("canonical_url") or source.original_url),
                        "read_status": read_status,
                        "response_id": str(read_result.get("_response_id") or ""),
                        "confidence": float(read_result.get("confidence") or 0),
                        "note": "Model-extracted URL/file view; use original/snapshot/evidence locators for final citations when available.",
                    },
                )
            )

    should_recover = not read_result or str(read_result.get("read_status") or "") not in {"readable", "partial"}
    if should_recover and settings.generation_v2_enable_openai_url_recovery:
        try:
            recovery = await asyncio.wait_for(
                _call_openai_url_recovery(source.original_url, source.metadata_ or {}),
                timeout=max(1, settings.generation_v2_openai_url_recovery_timeout_seconds),
            )
            source.metadata_ = {
                **(source.metadata_ or {}),
                "v2_openai_url_recovery": {
                    **recovery,
                    "response_id": str(recovery.get("_response_id") or ""),
                    "synced_at": datetime.utcnow().isoformat(),
                },
            }
        except Exception as exc:
            source.metadata_ = {
                **(source.metadata_ or {}),
                "v2_openai_url_recovery": {
                    "recovery_status": "error",
                    "error": str(exc),
                    "synced_at": datetime.utcnow().isoformat(),
                },
            }
    await db.flush()
    return versions


async def _call_openai_url_file_read(url: str) -> dict[str, Any]:
    client = get_openai_client()
    prompt = (
        "Read the attached external URL/file as a source for a podcast research corpus. "
        "Use only the attached file_url content. Do not rely on memory or external knowledge. "
        "If the file cannot be read, is blocked, is gone, or is unsupported, return that status and leave "
        "extracted_text_markdown empty. If readable, extract faithful article/document text in Markdown, "
        "preserving headings and useful locator cues. Do not invent missing text."
        f"\n\nURL: {url}"
    )
    response = await client.responses.create(
        model=settings.generation_v2_openai_model,
        instructions="You are a careful source-ingestion reader. Return JSON only.",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_file", "file_url": url},
                ],
            }
        ],
        max_output_tokens=settings.generation_v2_openai_url_read_max_output_tokens,
        text={
            "format": {
                "type": "json_schema",
                "name": "OpenAIUrlReadResult",
                "schema": _OPENAI_URL_READ_SCHEMA,
                "strict": True,
            }
        },
    )
    payload = _loads_json_object(getattr(response, "output_text", "") or "")
    payload["_response_id"] = getattr(response, "id", "")
    return payload


async def _call_openai_url_recovery(url: str, read_metadata: dict[str, Any]) -> dict[str, Any]:
    client = get_openai_client()
    prompt = (
        "The product tried to ingest this URL as source material, but direct URL/file reading did not produce "
        "usable source text. Use web search only to diagnose whether the URL is blocked/gone/paywalled and to "
        "find official or high-confidence alternate URLs for the same source. Do not summarize the topic broadly. "
        "Return only recovery metadata and candidate URLs; do not pretend a candidate is the same source unless the "
        "title/identifier clearly matches.\n\n"
        f"URL: {url}\n"
        f"Prior read metadata: {json.dumps(read_metadata.get('v2_openai_url_read', read_metadata), ensure_ascii=False)[:3000]}"
    )
    response = await client.responses.create(
        model=settings.generation_v2_openai_model,
        instructions="You are a conservative source recovery agent. Return JSON only.",
        input=prompt,
        tools=[{"type": "web_search"}],
        max_output_tokens=3000,
        text={
            "format": {
                "type": "json_schema",
                "name": "OpenAIUrlRecoveryResult",
                "schema": _OPENAI_URL_RECOVERY_SCHEMA,
                "strict": True,
            }
        },
    )
    output_text = getattr(response, "output_text", "") or ""
    if not output_text.strip():
        return {
            "recovery_status": "unclear",
            "original_url_status": "unknown",
            "best_alternative_url": "",
            "alternative_urls": [],
            "notes": "OpenAI web_search recovery returned an empty structured response.",
            "_response_id": getattr(response, "id", ""),
        }
    payload = _loads_json_object(output_text)
    payload["_response_id"] = getattr(response, "id", "")
    return payload


def _format_openai_url_read_document(url: str, payload: dict[str, Any], text: str) -> str:
    title = str(payload.get("title") or "Untitled source").strip()
    canonical_url = str(payload.get("canonical_url") or url).strip()
    summary = str(payload.get("summary") or "").strip()
    return (
        f"# {title}\n\n"
        f"Source URL: {url}\n"
        f"Canonical URL: {canonical_url}\n"
        f"OpenAI URL read status: {payload.get('read_status')}\n"
        f"Confidence: {payload.get('confidence')}\n\n"
        f"## OpenAI Read Summary\n\n{summary}\n\n"
        f"## Extracted Text\n\n{text}\n"
    )


def _loads_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("OpenAI URL read returned non-object JSON")
    return value


async def ensure_project_provider_index(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    run_id: uuid.UUID | None = None,
) -> ProviderCorpusIndex | None:
    # Serialize project-level provider index creation. URL bulk imports enqueue
    # many per-source native sync tasks at once, but there must be exactly one
    # OpenAI vector store row per project/provider/scope.
    await db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
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
    *,
    source_ids: set[uuid.UUID] | None = None,
) -> list[ProviderCorpusFile]:
    if (
        not provider_index.provider_vector_store_id
        or not settings.openai_api_key
        or not settings.generation_v2_enable_provider_uploads
    ):
        return []
    await _commit_if_supported(db)
    active_source_versions = [version for version in source_versions if version.is_active]
    agent_facing_versions = _agent_facing_provider_versions(active_source_versions)
    agent_facing_version_ids = {version.id for version in agent_facing_versions}
    await reconcile_provider_corpus_files(db, provider_index, source_versions, source_ids=source_ids)
    synced: list[ProviderCorpusFile] = []
    for version in agent_facing_versions:
        if not _version_uploadable(version):
            continue
        synced.append(await _ensure_provider_file(db, run, provider_index, version))
    ready_agent_source_ids = {
        provider_file.source_id
        for provider_file in synced
        if provider_file.source_version_id in agent_facing_version_ids and _provider_file_search_ready(provider_file)
    }
    await _detach_excluded_provider_files(
        db,
        provider_index,
        source_versions,
        agent_facing_version_ids,
        detach_source_ids=ready_agent_source_ids,
    )
    await db.flush()
    return synced


async def reconcile_provider_corpus_files(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
    *,
    source_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    if not provider_index.provider_vector_store_id or not settings.openai_api_key:
        return {}
    scope_source_ids = source_ids or {version.source_id for version in source_versions}
    versions_by_id = {version.id: version for version in source_versions}
    canonical_by_source = {
        source_id: canonical
        for source_id, versions in _versions_by_source(source_versions).items()
        if (canonical := _canonical_provider_version(versions)) is not None
    }
    file_row_conditions = [ProviderCorpusFile.provider_index_id == provider_index.id]
    if scope_source_ids:
        file_row_conditions.append(ProviderCorpusFile.source_id.in_(list(scope_source_ids)))
    file_rows_result = await db.execute(select(ProviderCorpusFile).where(*file_row_conditions))
    provider_rows = list(file_rows_result.scalars())
    rows_by_provider_file_id = {row.provider_file_id: row for row in provider_rows if row.provider_file_id}
    rows_by_version_id = {row.source_version_id: row for row in provider_rows}
    summary = {
        "adopted_count": 0,
        "repaired_count": 0,
        "invalid_orphan_count": 0,
        "detached_count": 0,
        "retained_stale_db_file_count": 0,
        "openai_active_agent_facing_file_count": 0,
        "pending_indexing_provider_file_count": 0,
    }
    vector_files = await _list_vector_store_files(provider_index)
    openai_file_ids = {str(getattr(vector_file, "id", "") or "") for vector_file in vector_files}
    for vector_file in vector_files:
        provider_file_id = str(getattr(vector_file, "id", "") or "")
        attributes = dict(getattr(vector_file, "attributes", None) or {})
        status = str(getattr(vector_file, "status", "") or "")
        validation = _validate_reconciliation_candidate(
            provider_index,
            vector_file,
            attributes,
            versions_by_id,
            canonical_by_source,
            scope_source_ids,
        )
        if validation.get("ignored"):
            continue
        version_id = _parse_uuid(attributes.get("source_version_id"))
        row = rows_by_provider_file_id.get(provider_file_id) or (
            rows_by_version_id.get(version_id) if version_id is not None else None
        )
        if not validation["valid"]:
            if row is not None and validation.get("reason") in {
                "source_version_not_active_ready",
                "noncanonical_source_version",
            }:
                summary["retained_stale_db_file_count"] += 1
                continue
            summary["invalid_orphan_count"] += 1
            try:
                await _detach_openai_vector_store_file(provider_index, provider_file_id)
                summary["detached_count"] += 1
            except Exception as exc:
                logger.info("Could not detach invalid OpenAI provider file %s: %s", provider_file_id, exc)
            continue
        version: SourceVersion = validation["version"]
        provider_attributes = _provider_attributes(version)
        row = row or rows_by_version_id.get(version.id)
        if (
            row
            and row.provider_file_id
            and row.provider_file_id != provider_file_id
            and row.status == V2RecordStatus.READY
            and row.provider_file_id in openai_file_ids
        ):
            summary["invalid_orphan_count"] += 1
            try:
                await _detach_openai_vector_store_file(provider_index, provider_file_id)
                summary["detached_count"] += 1
            except Exception as exc:
                logger.info("Could not detach duplicate OpenAI provider file %s: %s", provider_file_id, exc)
            continue
        is_new_row = row is None
        if row is None:
            row = ProviderCorpusFile(
                provider_index_id=provider_index.id,
                source_version_id=version.id,
                source_id=version.source_id,
                provider="openai",
                status=V2RecordStatus.PENDING,
            )
            db.add(row)
        row.provider_file_id = provider_file_id
        row.provider_vector_store_file_id = provider_file_id
        row.attributes = _provider_db_attributes(
            row.attributes,
            _PROVIDER_PHASE_ADOPTED if status == "completed" else _PROVIDER_PHASE_INDEXING,
            provider_attributes,
            last_provider_status=status or "unknown",
            last_reconciled_at=datetime.utcnow().isoformat(),
            adoption_source="vector_store_reconciliation",
        )
        if status == "completed":
            row.status = V2RecordStatus.READY
            row.error_message = None
            row.cleanup_status = None
            version.provider = "openai"
            version.provider_file_id = provider_file_id
            version.provider_vector_store_file_id = provider_file_id
            version.upload_status = V2RecordStatus.READY
            summary["openai_active_agent_facing_file_count"] += 1
            summary["adopted_count" if is_new_row else "repaired_count"] += 1
        elif status in {"failed", "cancelled"}:
            row.status = V2RecordStatus.FAILED
            row.error_message = _provider_last_error(vector_file) or f"OpenAI vector store file status: {status}"
            row.attributes = _provider_db_attributes(
                row.attributes,
                _PROVIDER_PHASE_FAILED,
                provider_attributes,
                last_provider_status=status,
                last_openai_error=row.error_message,
            )
            version.upload_status = V2RecordStatus.FAILED
        else:
            row.status = V2RecordStatus.PENDING
            row.error_message = None
            version.provider = "openai"
            version.provider_file_id = provider_file_id
            version.provider_vector_store_file_id = provider_file_id
            version.upload_status = V2RecordStatus.PENDING
            summary["pending_indexing_provider_file_count"] += 1
            summary["adopted_count" if is_new_row else "repaired_count"] += 1
        await db.flush()
    provider_index.config = {
        **(provider_index.config or {}),
        "last_reconciliation": {
            **summary,
            "last_reconciliation_at": datetime.utcnow().isoformat(),
        },
    }
    await db.flush()
    await _commit_if_supported(db)
    return summary


async def sync_source_to_project_corpus_now(db: AsyncSession, source: Source) -> dict[str, Any]:
    """Sync one source into the project-level V2/OpenAI corpus.

    This is the native-first source preparation path. It is intentionally scoped
    to one source so bulk URL imports do not rescan or refetch the whole project
    after every completed deterministic ingest task.
    """
    source_id = source.id
    project_id = source.project_id
    provider_index = await ensure_project_provider_index(db, project_id)
    # Release the project/index creation lock before potentially slow network
    # work. Source version fetches and OpenAI vector-store indexing may take
    # minutes; holding the project row lock across that would serialize or
    # deadlock bulk imports.
    await db.commit()
    source_result = await db.execute(select(Source).where(Source.id == source_id))
    source = source_result.scalar_one()
    versions = await ensure_source_versions(db, source)
    await _commit_if_supported(db)
    active_versions = [version for version in versions if version.is_active and version.status == V2RecordStatus.READY]
    sync_versions = await _load_ready_source_versions_for_sources(db, project_id, [source.id])
    provider_files = (
        await sync_provider_corpus_files(db, None, provider_index, sync_versions, source_ids={source.id})
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


async def _load_project_corpus_sources(db: AsyncSession, project_id: uuid.UUID) -> list[Source]:
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


async def reconcile_project_corpus_index_now(db: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    """Repair the provider corpus index from already-materialized source versions.

    This is the operational `/corpus-index/sync` path. It intentionally does not
    fetch URLs, render snapshots, or create SourceVersion rows; ingest/sync code
    owns those source-side mutations.
    """
    sources = await _load_project_corpus_sources(db, project_id)
    source_ids = {source.id for source in sources}
    ready_versions = await _load_ready_source_versions_for_sources(db, project_id, list(source_ids))
    active_versions = [version for version in ready_versions if version.is_active]
    provider_index = await ensure_project_provider_index(db, project_id)
    await _commit_if_supported(db)
    if not provider_index:
        return {"source_count": len(sources), "source_version_count": len(active_versions), "provider_file_count": 0}
    provider_files = await sync_provider_corpus_files(
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


async def sync_project_corpus_index_now(db: AsyncSession, project_id: uuid.UUID) -> None:
    sources = await _load_project_corpus_sources(db, project_id)
    versions: list[SourceVersion] = []
    for source in sources:
        versions.extend(await ensure_source_versions(db, source))
    await _commit_if_supported(db)
    active_versions = [version for version in versions if version.is_active and version.status == V2RecordStatus.READY]
    provider_index = await ensure_project_provider_index(db, project_id)
    await _commit_if_supported(db)
    if provider_index:
        await sync_provider_corpus_files(db, None, provider_index, active_versions, source_ids={source.id for source in sources})
    await db.flush()


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
    eligible_provider_file_ids = [file.provider_file_id for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY]
    provider_ready_source_ids = {str(file.source_id) for file in provider_files if file.provider_file_id and file.status == V2RecordStatus.READY}
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
    if not readiness["source_version_count"] or not readiness["usable_source_count"]:
        status = V2RecordStatus.FAILED
    else:
        status = V2RecordStatus.READY
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
    source_result = await db.execute(select(Source).where(Source.project_id == project_id).order_by(Source.created_at.asc()))
    sources = list(source_result.scalars())
    files: list[ProviderCorpusFile] = []
    if index:
        file_result = await db.execute(select(ProviderCorpusFile).where(ProviderCorpusFile.provider_index_id == index.id))
        files = list(file_result.scalars())
    active_ready_versions = [
        version for version in versions if version.is_active and version.status == V2RecordStatus.READY
    ]
    ready_provider_files = [file for file in files if file.status == V2RecordStatus.READY]
    provider_by_version = {file.source_version_id: file for file in ready_provider_files if _provider_file_search_ready(file)}
    versions_by_source = _versions_by_source(active_ready_versions)
    source_summaries: list[dict[str, Any]] = []
    canonical_file_ids: set[str] = set()
    for source in sources:
        source_versions = versions_by_source.get(source.id, [])
        canonical = _canonical_provider_version(source_versions)
        provider_file = provider_by_version.get(canonical.id) if canonical else None
        skipped_reason: str | None = None
        if not source_versions:
            skipped_reason = "no_ready_source_versions"
        elif canonical is None:
            skipped_reason = "no_canonical_source_version"
        elif _provider_uploads_required() and (not index or not index.provider_vector_store_id or index.status != V2RecordStatus.READY):
            skipped_reason = "provider_index_not_ready"
        elif _provider_uploads_required() and provider_file is None:
            skipped_reason = "canonical_provider_file_not_ready"
        if provider_file and provider_file.provider_file_id:
            canonical_file_ids.add(provider_file.provider_file_id)
        source_summaries.append(
            {
                "source_id": str(source.id),
                "source_type": str(source.source_type),
                "canonical_version_id": str(canonical.id) if canonical else None,
                "canonical_kind": str(canonical.kind) if canonical else None,
                "provider_file_id": provider_file.provider_file_id if provider_file else None,
                "provider_status": provider_file.status if provider_file else None,
                "upload_phase": (provider_file.attributes or {}).get("upload_phase") if provider_file else None,
                "skipped_reason": skipped_reason,
            }
        )
    agent_facing_files = [
        file
        for file in ready_provider_files
        if (file.attributes or {}).get("is_active") is True and (file.attributes or {}).get("agent_facing") is True
    ]
    pending_indexing_files = [
        file
        for file in files
        if file.status == V2RecordStatus.PENDING and (file.attributes or {}).get("upload_phase") == _PROVIDER_PHASE_INDEXING
    ]
    failed_provider_files = [file for file in files if file.status == V2RecordStatus.FAILED]
    last_reconciliation = (index.config or {}).get("last_reconciliation", {}) if index else {}
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
        "ready_provider_file_count": len(ready_provider_files),
        "canonical_provider_file_count": len(canonical_file_ids),
        "active_agent_facing_provider_file_count": len(agent_facing_files),
        "noncanonical_provider_file_count": len(
            [file for file in agent_facing_files if file.provider_file_id not in canonical_file_ids]
        ),
        "db_canonical_provider_file_count": len(canonical_file_ids),
        "openai_active_agent_facing_file_count": int(
            last_reconciliation.get("openai_active_agent_facing_file_count", len(agent_facing_files)) or 0
        ),
        "pending_indexing_provider_file_count": len(pending_indexing_files),
        "failed_provider_file_count": len(failed_provider_files),
        "adoptable_orphan_count": int(last_reconciliation.get("adopted_count", 0) or 0),
        "invalid_orphan_count": int(last_reconciliation.get("invalid_orphan_count", 0) or 0),
        "last_reconciliation_at": last_reconciliation.get("last_reconciliation_at"),
        "sources": source_summaries,
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
        response = await _fetch_url_for_v2_snapshot(source.original_url)
        content_type = response.headers.get("content-type", "")
        if _is_pdf_response(source.original_url, content_type, response.content):
            content_hash = _hash_bytes(response.content)
            filename = _filename_from_url(str(response.url), default=f"{source.id}.pdf", required_suffix=".pdf")
            object_name = _versioned_object_name(source.id, content_hash, filename)
            upload_file(get_minio_client(), response.content, object_name, content_type or "application/pdf")
            versions.append(
                await _ensure_source_version(
                    db,
                    source,
                    kind=SourceRepresentationKind.ORIGINAL_FILE,
                    storage_path=object_name,
                    content_hash=content_hash,
                    metadata={
                        "canonical": True,
                        "source_type": "pdf",
                        "original_filename": filename,
                        "native_upload": True,
                        "fetched_from_url": True,
                        "url": str(response.url),
                        "status_code": response.status_code,
                        "content_type": content_type,
                    },
                )
            )
            return versions
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
                    "source_type": "web",
                    "native_upload": True,
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "content_type": content_type,
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


async def _fetch_url_for_v2_snapshot(url: str):
    from app.ingestion.web import _safe_fetch

    timeout_seconds = max(1, settings.web_fetch_timeout + 5)
    try:
        return await asyncio.wait_for(_safe_fetch(url), timeout=timeout_seconds)
    except Exception as first_exc:
        browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
            "Accept-Language": settings.web_accept_language,
        }
        try:
            return await asyncio.wait_for(_safe_fetch(url, headers=browser_headers), timeout=timeout_seconds)
        except Exception:
            raise first_exc


def _is_pdf_response(url: str, content_type: str, content: bytes) -> bool:
    normalized_type = (content_type or "").lower()
    path = urlparse(url).path.lower()
    if "application/pdf" in normalized_type:
        return True
    if content[:1024].lstrip().startswith(b"%PDF"):
        return True
    if path.endswith(".pdf") and (not normalized_type or "octet-stream" in normalized_type):
        return True
    return False


def _filename_from_url(url: str, *, default: str, required_suffix: str | None = None) -> str:
    path_name = Path(unquote(urlparse(url).path)).name
    filename = path_name or default
    if "." not in filename:
        filename = default
    if required_suffix:
        normalized_suffix = required_suffix if required_suffix.startswith(".") else f".{required_suffix}"
        if Path(filename).suffix.lower() != normalized_suffix.lower():
            filename = f"{filename}{normalized_suffix}"
    return filename.replace("/", "_")


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
    if previous:
        previous.is_active = False
        version.supersedes_source_version_id = previous.id
        await db.flush()
    return version


async def _commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()


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
        await db.flush()
        await _commit_if_supported(db)
        await _detach_provider_file_from_active_index(db, provider_file)
        await db.flush()
        await _commit_if_supported(db)
    await db.flush()


async def _detach_provider_file_from_active_index(db: AsyncSession, provider_file: ProviderCorpusFile) -> None:
    if not settings.openai_api_key or not provider_file.provider_file_id:
        return
    provider_index = await db.get(ProviderCorpusIndex, provider_file.provider_index_id)
    if not provider_index or not provider_index.provider_vector_store_id:
        return
    provider_file.attributes = {
        **(provider_file.attributes or {}),
        "is_active": False,
        "agent_facing": False,
        "canonical_retrieval_surface": False,
        "retrieval_surface": "fallback",
        "upload_phase": _PROVIDER_PHASE_DETACHED,
    }
    await db.flush()
    await _commit_if_supported(db)
    try:
        await _detach_openai_vector_store_file(provider_index, provider_file.provider_file_id)
        provider_file.cleanup_status = "removed_from_active_index_retained"
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
    await db.flush()
    await _commit_if_supported(db)


async def _detach_openai_vector_store_file(provider_index: ProviderCorpusIndex, provider_file_id: str) -> None:
    if not settings.openai_api_key or not provider_index.provider_vector_store_id or not provider_file_id:
        return
    client = get_openai_client()
    await asyncio.wait_for(
        client.vector_stores.files.delete(
            vector_store_id=provider_index.provider_vector_store_id,
            file_id=provider_file_id,
        ),
        timeout=_provider_vector_operation_timeout_seconds(),
    )


async def _ensure_provider_file(
    db: AsyncSession,
    run: GenerationRun | None,
    provider_index: ProviderCorpusIndex,
    version: SourceVersion,
) -> ProviderCorpusFile:
    provider_attributes = _provider_attributes(version)
    existing_result = await db.execute(
        select(ProviderCorpusFile).where(
            ProviderCorpusFile.provider_index_id == provider_index.id,
            ProviderCorpusFile.source_version_id == version.id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing and existing.status == V2RecordStatus.READY and existing.provider_file_id:
        if await _sync_provider_file_attributes(provider_index, existing, provider_attributes):
            existing.attributes = _provider_db_attributes(existing.attributes, _PROVIDER_PHASE_READY, provider_attributes)
            existing.cleanup_status = None
            version.provider = "openai"
            version.provider_file_id = existing.provider_file_id
            version.provider_vector_store_file_id = existing.provider_vector_store_file_id
            version.upload_status = V2RecordStatus.READY
            await db.flush()
            await _commit_if_supported(db)
            return existing
        existing.status = V2RecordStatus.PENDING
        existing.error_message = "Provider file attributes were stale and could not be updated; re-uploading"
    if existing is None:
        existing = ProviderCorpusFile(
            provider_index_id=provider_index.id,
            source_version_id=version.id,
            source_id=version.source_id,
            provider="openai",
            attributes=_provider_db_attributes({}, _PROVIDER_PHASE_DB_PREPARED, provider_attributes),
            status=V2RecordStatus.PENDING,
        )
        db.add(existing)
        await db.flush()
        await _commit_if_supported(db)
    try:
        client = get_openai_client()
        filename: str | None = None
        if not existing.provider_file_id:
            filename, content = _version_upload_payload(version)
            if not content:
                raise RuntimeError("Source version content is empty")
            file_obj = await asyncio.wait_for(
                client.files.create(file=(filename, io.BytesIO(content)), purpose="assistants"),
                timeout=max(30, int(settings.generation_v2_provider_upload_timeout_seconds)),
            )
            existing.provider_file_id = file_obj.id
            existing.status = V2RecordStatus.PENDING
            existing.error_message = None
            existing.attributes = _provider_db_attributes(
                existing.attributes,
                _PROVIDER_PHASE_FILE_UPLOADED,
                provider_attributes,
                last_provider_status="file_uploaded",
            )
            version.provider = "openai"
            version.provider_file_id = file_obj.id
            version.upload_status = V2RecordStatus.PENDING
            await db.flush()
            await _commit_if_supported(db)
        vector_file = await _ensure_vector_store_file_attached(client, provider_index, existing, provider_attributes)
        if vector_file is not None:
            existing.provider_vector_store_file_id = getattr(vector_file, "id", None) or existing.provider_file_id
            existing.attributes = _provider_db_attributes(
                existing.attributes,
                _PROVIDER_PHASE_ATTACHED,
                provider_attributes,
                last_provider_status=str(getattr(vector_file, "status", "") or "attached"),
            )
            version.provider_vector_store_file_id = existing.provider_vector_store_file_id
            await db.flush()
            await _commit_if_supported(db)
        await _poll_provider_file_until_terminal(db, client, provider_index, existing, version, provider_attributes)
        if run is not None:
            await save_tool_trace(
                db,
                generation_run_id=run.id,
                phase="v2_corpus_prepare",
                agent_name="source_corpus",
                tool_name="openai.files.create/vector_store_file.resume",
                input_json={"source_version_id": str(version.id), "filename": filename, "attributes": provider_attributes},
                output_json={
                    "provider_file_id": existing.provider_file_id,
                    "provider_vector_store_file_id": existing.provider_vector_store_file_id,
                    "status": existing.status,
                    "upload_phase": (existing.attributes or {}).get("upload_phase"),
                },
                provider_call_id=existing.provider_file_id,
            )
    except Exception as exc:
        logger.info("Provider corpus file sync failed for %s: %s", version.id, exc, exc_info=True)
        existing.status = V2RecordStatus.FAILED
        existing.error_message = str(exc)
        existing.attributes = _provider_db_attributes(
            existing.attributes,
            _PROVIDER_PHASE_FAILED,
            provider_attributes,
            last_openai_error=str(exc),
        )
        version.upload_status = V2RecordStatus.FAILED
    await db.flush()
    await _commit_if_supported(db)
    return existing


async def _ensure_vector_store_file_attached(
    client: Any,
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    attributes: dict[str, str | bool | float],
) -> Any | None:
    if not provider_index.provider_vector_store_id or not provider_file.provider_file_id:
        return None
    existing = await _retrieve_vector_store_file(client, provider_index, provider_file.provider_file_id)
    if existing is not None:
        return existing
    try:
        return await asyncio.wait_for(
            client.vector_stores.files.create(
                vector_store_id=provider_index.provider_vector_store_id,
                file_id=provider_file.provider_file_id,
                attributes=attributes,
            ),
            timeout=_provider_vector_operation_timeout_seconds(),
        )
    except Exception:
        existing = await _retrieve_vector_store_file(client, provider_index, provider_file.provider_file_id)
        if existing is not None:
            return existing
        raise


async def _poll_provider_file_until_terminal(
    db: AsyncSession,
    client: Any,
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    version: SourceVersion,
    attributes: dict[str, str | bool | float],
) -> None:
    deadline = asyncio.get_running_loop().time() + max(30, int(settings.generation_v2_provider_upload_timeout_seconds))
    while True:
        vector_file = await _retrieve_vector_store_file(client, provider_index, provider_file.provider_file_id or "")
        if vector_file is None:
            provider_file.status = V2RecordStatus.PENDING
            provider_file.error_message = None
            provider_file.attributes = _provider_db_attributes(
                provider_file.attributes,
                _PROVIDER_PHASE_INDEXING,
                attributes,
                last_provider_status="retrieve_unavailable",
                last_openai_error="provider_vector_store_file_retrieve_unavailable",
            )
            version.upload_status = V2RecordStatus.PENDING
            await db.flush()
            await _commit_if_supported(db)
            return
        last_status = str(getattr(vector_file, "status", "") or "")
        provider_file.provider_vector_store_file_id = getattr(vector_file, "id", None) or provider_file.provider_file_id
        if last_status == "completed":
            provider_file.status = V2RecordStatus.READY
            provider_file.error_message = None
            provider_file.cleanup_status = None
            provider_file.attributes = _provider_db_attributes(
                provider_file.attributes,
                _PROVIDER_PHASE_READY,
                attributes,
                last_provider_status=last_status,
            )
            version.provider = "openai"
            version.provider_file_id = provider_file.provider_file_id
            version.provider_vector_store_file_id = provider_file.provider_vector_store_file_id
            version.upload_status = V2RecordStatus.READY
            await db.flush()
            await _commit_if_supported(db)
            return
        if last_status in {"failed", "cancelled"}:
            last_error = _provider_last_error(vector_file) or f"OpenAI vector store file status: {last_status}"
            provider_file.status = V2RecordStatus.FAILED
            provider_file.error_message = last_error
            provider_file.attributes = _provider_db_attributes(
                provider_file.attributes,
                _PROVIDER_PHASE_FAILED,
                attributes,
                last_provider_status=last_status,
                last_openai_error=last_error,
            )
            version.upload_status = V2RecordStatus.FAILED
            await db.flush()
            await _commit_if_supported(db)
            return
        if asyncio.get_running_loop().time() >= deadline:
            provider_file.status = V2RecordStatus.PENDING
            provider_file.error_message = None
            provider_file.attributes = _provider_db_attributes(
                provider_file.attributes,
                _PROVIDER_PHASE_INDEXING,
                attributes,
                last_provider_status=last_status or "in_progress",
                last_openai_error="provider_indexing_timeout",
            )
            version.provider = "openai"
            version.provider_file_id = provider_file.provider_file_id
            version.provider_vector_store_file_id = provider_file.provider_vector_store_file_id
            version.upload_status = V2RecordStatus.PENDING
            await db.flush()
            await _commit_if_supported(db)
            return
        provider_file.status = V2RecordStatus.PENDING
        provider_file.attributes = _provider_db_attributes(
            provider_file.attributes,
            _PROVIDER_PHASE_INDEXING,
            attributes,
            last_provider_status=last_status or "in_progress",
        )
        await db.flush()
        await _commit_if_supported(db)
        await asyncio.sleep(1)


async def _retrieve_vector_store_file(client: Any, provider_index: ProviderCorpusIndex, provider_file_id: str) -> Any | None:
    if not provider_index.provider_vector_store_id or not provider_file_id:
        return None
    try:
        return await asyncio.wait_for(
            client.vector_stores.files.retrieve(
                file_id=provider_file_id,
                vector_store_id=provider_index.provider_vector_store_id,
            ),
            timeout=_provider_vector_operation_timeout_seconds(),
        )
    except Exception:
        return None


async def _sync_provider_file_attributes(
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    attributes: dict[str, str | bool | float],
) -> bool:
    if (
        not settings.openai_api_key
        or not provider_index.provider_vector_store_id
        or not provider_file.provider_file_id
        or _provider_external_attributes(provider_file.attributes or {}) == attributes
    ):
        return True
    try:
        client = get_openai_client()
        await asyncio.wait_for(
            client.vector_stores.files.update(
                file_id=provider_file.provider_file_id,
                vector_store_id=provider_index.provider_vector_store_id,
                attributes=attributes,
            ),
            timeout=_provider_vector_operation_timeout_seconds(),
        )
        return True
    except Exception as exc:
        logger.info("Could not update provider corpus file attributes %s: %s", provider_file.id, exc)
        return False


def _version_uploadable(version: SourceVersion) -> bool:
    return bool(version.storage_path and version.kind != SourceRepresentationKind.ORIGINAL_URL and version.status == V2RecordStatus.READY)


_CANONICAL_RETRIEVAL_PRIORITY = {
    SourceRepresentationKind.ORIGINAL_FILE: 10,
    SourceRepresentationKind.TRANSCRIPT: 20,
    SourceRepresentationKind.EXTRACTED_TEXT: 30,
    SourceRepresentationKind.RENDERED_SNAPSHOT: 40,
}


def _agent_facing_provider_versions(source_versions: list[SourceVersion]) -> list[SourceVersion]:
    """Return the single canonical provider-search version per source."""
    ready_versions = [version for version in source_versions if _version_uploadable(version)]
    agent_versions: list[SourceVersion] = []
    for versions in _versions_by_source(ready_versions).values():
        canonical = _canonical_provider_version(versions)
        if canonical is not None:
            agent_versions.append(canonical)
    return agent_versions


def _canonical_provider_version(source_versions: list[SourceVersion]) -> SourceVersion | None:
    candidates: list[tuple[int, datetime, str, SourceVersion]] = []
    for version in source_versions:
        if not _version_uploadable(version):
            continue
        try:
            kind = SourceRepresentationKind(str(version.kind))
        except ValueError:
            continue
        priority = _CANONICAL_RETRIEVAL_PRIORITY.get(kind)
        if priority is None:
            continue
        candidates.append((priority, version.created_at or datetime.min, str(version.id), version))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def _versions_by_source(source_versions: list[SourceVersion]) -> dict[uuid.UUID, list[SourceVersion]]:
    grouped: dict[uuid.UUID, list[SourceVersion]] = {}
    for version in source_versions:
        grouped.setdefault(version.source_id, []).append(version)
    return grouped


def _provider_uploads_required() -> bool:
    return bool(settings.openai_api_key and settings.generation_v2_enable_provider_uploads)


def _provider_vector_operation_timeout_seconds() -> int:
    return max(10, min(60, int(settings.generation_v2_provider_upload_timeout_seconds)))


def _provider_file_search_ready(provider_file: ProviderCorpusFile) -> bool:
    attributes = provider_file.attributes or {}
    return bool(
        provider_file.provider_file_id
        and provider_file.status == V2RecordStatus.READY
        and attributes.get("is_active", True) is True
        and attributes.get("agent_facing", True) is True
        and attributes.get("canonical_retrieval_surface", True) is True
    )


def _version_kind_counts(source_versions) -> dict[str, int]:
    counts: dict[str, int] = {}
    for version in source_versions:
        key = str(version.kind)
        counts[key] = counts.get(key, 0) + 1
    return counts


async def _detach_excluded_provider_files(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
    agent_facing_version_ids: set[uuid.UUID],
    *,
    detach_source_ids: set[uuid.UUID] | None = None,
) -> None:
    if detach_source_ids is not None and not detach_source_ids:
        return
    candidate_ids = [
        version.id
        for version in source_versions
        if _version_uploadable(version) and (detach_source_ids is None or version.source_id in detach_source_ids)
    ]
    if not candidate_ids:
        return
    result = await db.execute(
        select(ProviderCorpusFile).where(
            ProviderCorpusFile.provider_index_id == provider_index.id,
            ProviderCorpusFile.source_version_id.in_(candidate_ids),
        )
    )
    for provider_file in result.scalars():
        if provider_file.source_version_id in agent_facing_version_ids:
            continue
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "agent_facing": False,
            "canonical_retrieval_surface": False,
            "retrieval_surface": "fallback",
            "excluded_reason": "noncanonical_retrieval_surface",
        }
        provider_file.cleanup_status = "excluded_from_agent_facing_index"
        await db.flush()
        await _commit_if_supported(db)
        await _detach_provider_file_from_active_index(db, provider_file)


def _version_has_snapshot(version: SourceVersion) -> bool:
    return bool(
        version.storage_path
        and version.status == V2RecordStatus.READY
        and version.kind
        in {
            SourceRepresentationKind.FETCHED_HTML,
            SourceRepresentationKind.RENDERED_SNAPSHOT,
            SourceRepresentationKind.MODEL_READ_TEXT,
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
        if metadata.get("source_type") == "pdf" and Path(filename).suffix.lower() != ".pdf":
            filename = f"{filename}.pdf"
    else:
        filename = f"{version.source_id}-{version.kind}-{version.id}{suffix or ''}"
    return filename.replace("/", "_")


async def _list_vector_store_files(provider_index: ProviderCorpusIndex) -> list[Any]:
    if not settings.openai_api_key or not provider_index.provider_vector_store_id:
        return []
    client = get_openai_client()

    async def _collect() -> list[Any]:
        listing = client.vector_stores.files.list(
            vector_store_id=provider_index.provider_vector_store_id,
            limit=100,
        )
        if hasattr(listing, "__await__"):
            listing = await listing
        if hasattr(listing, "__aiter__"):
            items: list[Any] = []
            async for item in listing:
                items.append(item)
            return items
        if hasattr(listing, "data"):
            return list(listing.data)
        return list(listing)

    return await asyncio.wait_for(_collect(), timeout=_provider_vector_operation_timeout_seconds())


def _validate_reconciliation_candidate(
    provider_index: ProviderCorpusIndex,
    vector_file: Any,
    attributes: dict[str, Any],
    versions_by_id: dict[uuid.UUID, SourceVersion],
    canonical_by_source: dict[uuid.UUID, SourceVersion],
    scope_source_ids: set[uuid.UUID],
) -> dict[str, Any]:
    provider_file_id = str(getattr(vector_file, "id", "") or "")
    if not provider_file_id:
        return {"valid": False, "reason": "missing_provider_file_id"}
    project_id = str(attributes.get("project_id") or "")
    if project_id and project_id != str(provider_index.project_id):
        return {"valid": False, "reason": "wrong_project"}
    source_id = _parse_uuid(attributes.get("source_id"))
    if source_id is None:
        if len(scope_source_ids) <= 1:
            return {"valid": False, "ignored": True, "reason": "source_outside_sync_scope"}
        return {"valid": False, "reason": "missing_source_id"}
    if source_id not in scope_source_ids:
        return {"valid": False, "ignored": True, "reason": "source_outside_sync_scope"}
    version_id = _parse_uuid(attributes.get("source_version_id"))
    if version_id is None:
        return {"valid": False, "reason": "missing_source_version_id"}
    version = versions_by_id.get(version_id)
    if version is None:
        return {"valid": False, "reason": "missing_source_version"}
    if version.source_id != source_id:
        return {"valid": False, "reason": "source_version_source_mismatch"}
    if not version.is_active or version.status != V2RecordStatus.READY or not _version_uploadable(version):
        return {"valid": False, "reason": "source_version_not_active_ready"}
    canonical = canonical_by_source.get(version.source_id)
    if canonical is None or canonical.id != version.id:
        return {"valid": False, "reason": "noncanonical_source_version"}
    representation_kind = str(attributes.get("representation_kind") or "")
    if representation_kind and representation_kind != str(version.kind):
        return {"valid": False, "reason": "representation_kind_mismatch"}
    content_hash = str(attributes.get("content_hash") or "")
    if content_hash and version.content_hash and content_hash != version.content_hash:
        return {"valid": False, "reason": "content_hash_mismatch"}
    return {"valid": True, "version": version}


def _parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _provider_db_attributes(
    current: dict[str, Any] | None,
    phase: str,
    provider_attributes: dict[str, str | bool | float],
    **extra: Any,
) -> dict[str, Any]:
    merged = dict(current or {})
    if phase in {_PROVIDER_PHASE_ADOPTED, _PROVIDER_PHASE_READY}:
        for stale_key in ("active_index_status", "excluded_reason", "last_openai_error"):
            merged.pop(stale_key, None)
    return {
        **merged,
        **provider_attributes,
        "upload_phase": phase,
        **{key: value for key, value in extra.items() if value is not None},
    }


def _provider_external_attributes(attributes: dict[str, Any]) -> dict[str, str | bool | float]:
    return {
        key: value
        for key, value in attributes.items()
        if key not in _PROVIDER_INTERNAL_ATTRIBUTE_KEYS and isinstance(value, (str, bool, float, int))
    }


def _provider_last_error(vector_file: Any) -> str | None:
    last_error = getattr(vector_file, "last_error", None)
    if isinstance(last_error, dict):
        return str(last_error.get("message") or last_error.get("code") or "") or None
    message = getattr(last_error, "message", None)
    code = getattr(last_error, "code", None)
    if message or code:
        return str(message or code)
    return None


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
        "agent_facing": True,
        "canonical_retrieval_surface": True,
        "retrieval_surface": "canonical",
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
