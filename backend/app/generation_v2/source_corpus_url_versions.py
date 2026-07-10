from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.source_corpus_contracts import (
    OpenAIUrlReadStatus,
    OpenAIUrlRecoveryStatus,
    OPENAI_URL_READ_SUCCESS_STATUSES,
)
from app.generation_v2.source_corpus_helpers import hash_text as _hash_text, versioned_object_name as _versioned_object_name
from app.generation_v2.source_corpus_url_read import (
    call_openai_url_file_read,
    call_openai_url_recovery,
    format_openai_url_read_document,
)
from app.generation_v2.source_corpus_version_persistence import ensure_source_version
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source
from app.services.llm import get_openai_client
from app.services.storage import get_minio_client, upload_text


@dataclass(frozen=True)
class OpenAIUrlReadVersionDeps:
    call_openai_url_file_read: Any
    call_openai_url_recovery: Any
    format_openai_url_read_document: Any
    get_minio_client: Any
    upload_text: Any
    ensure_source_version: Any


def default_openai_url_read_version_deps() -> OpenAIUrlReadVersionDeps:
    return OpenAIUrlReadVersionDeps(
        call_openai_url_file_read=call_openai_url_file_read_with_client,
        call_openai_url_recovery=call_openai_url_recovery_with_client,
        format_openai_url_read_document=format_openai_url_read_document,
        get_minio_client=get_minio_client,
        upload_text=upload_text,
        ensure_source_version=ensure_source_version,
    )


async def ensure_openai_url_read_versions(
    db: AsyncSession,
    source: Source,
    *,
    deps: OpenAIUrlReadVersionDeps | None = None,
) -> list[SourceVersion]:
    deps = deps or default_openai_url_read_version_deps()
    versions: list[SourceVersion] = []
    if not source.original_url or not settings.generation_v2_enable_openai_url_read or not settings.openai_api_key:
        return versions
    existing_versions = await _existing_ready_model_read_versions(db, source)
    if existing_versions:
        return existing_versions

    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
    prior_read = metadata.get("v2_openai_url_read")
    if isinstance(prior_read, dict) and prior_read.get("synced_at"):
        return versions

    read_result = await _try_openai_url_read(source, deps)
    if read_result:
        versions.extend(await _persist_openai_url_read_version(db, source, read_result, deps))

    should_recover = not read_result or str(read_result.get("read_status") or "") not in OPENAI_URL_READ_SUCCESS_STATUSES
    if should_recover and settings.generation_v2_enable_openai_url_recovery:
        await _try_openai_url_recovery(source, deps)
    await db.flush()
    return versions


async def call_openai_url_file_read_with_client(url: str) -> dict[str, Any]:
    return await call_openai_url_file_read(url, client=get_openai_client())


async def call_openai_url_recovery_with_client(url: str, read_metadata: dict[str, Any]) -> dict[str, Any]:
    return await call_openai_url_recovery(url, read_metadata, client=get_openai_client())


async def _existing_ready_model_read_versions(db: AsyncSession, source: Source) -> list[SourceVersion]:
    existing_result = await db.execute(
        select(SourceVersion).where(
            SourceVersion.source_id == source.id,
            SourceVersion.kind == SourceRepresentationKind.MODEL_READ_TEXT,
            SourceVersion.is_active.is_(True),
            SourceVersion.status == V2RecordStatus.READY,
        )
    )
    return list(existing_result.scalars())


async def _try_openai_url_read(source: Source, deps: OpenAIUrlReadVersionDeps) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(
            deps.call_openai_url_file_read(source.original_url),
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
        return None


async def _persist_openai_url_read_version(
    db: AsyncSession,
    source: Source,
    read_result: dict[str, Any],
    deps: OpenAIUrlReadVersionDeps,
) -> list[SourceVersion]:
    versions: list[SourceVersion] = []
    text = str(read_result.get("extracted_text_markdown") or "").strip()
    read_status = str(read_result.get("read_status") or OpenAIUrlReadStatus.ERROR)
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
    if len(text) < settings.generation_v2_openai_url_read_min_chars or read_status not in OPENAI_URL_READ_SUCCESS_STATUSES:
        return versions
    document = deps.format_openai_url_read_document(source.original_url, read_result, text)
    content_hash = _hash_text(document)
    object_name = _versioned_object_name(source.id, content_hash, "openai_url_read.md")
    deps.upload_text(deps.get_minio_client(), document, object_name)
    versions.append(
        await deps.ensure_source_version(
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
    return versions


async def _try_openai_url_recovery(source: Source, deps: OpenAIUrlReadVersionDeps) -> None:
    try:
        recovery = await asyncio.wait_for(
            deps.call_openai_url_recovery(source.original_url, source.metadata_ or {}),
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
                "recovery_status": OpenAIUrlRecoveryStatus.ERROR,
                "error": str(exc),
                "synced_at": datetime.utcnow().isoformat(),
            },
        }


__all__ = [
    "OpenAIUrlReadVersionDeps",
    "call_openai_url_file_read_with_client",
    "call_openai_url_recovery_with_client",
    "default_openai_url_read_version_deps",
    "ensure_openai_url_read_versions",
]
