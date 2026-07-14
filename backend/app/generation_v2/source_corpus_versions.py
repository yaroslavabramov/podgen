from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus_web_snapshot import (
    ensure_web_snapshot_versions,
    fetch_url_for_v2_snapshot,
)
from app.generation_v2.source_corpus_url_versions import (
    OpenAIUrlReadVersionDeps,
    call_openai_url_file_read_with_client,
    call_openai_url_recovery_with_client,
    default_openai_url_read_version_deps,
    ensure_openai_url_read_versions,
)
from app.generation_v2.source_corpus_version_persistence import ensure_source_version
from app.generation_v2.source_corpus_helpers import (
    hash_bytes as _hash_bytes,
    hash_text as _hash_text,
    page_mark_text as _page_mark_text,
    versioned_object_name as _versioned_object_name,
)
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source
from app.services.storage import download_file, download_text, get_minio_client, upload_text

logger = logging.getLogger(__name__)

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
            await ensure_source_version(
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
            await ensure_source_version(
                db,
                source,
                kind=SourceRepresentationKind.ORIGINAL_URL,
                storage_path=source.original_url,
                content_hash=_hash_text(source.original_url),
                metadata={"canonical": True, "url": source.original_url},
            )
        )
        versions.extend(await ensure_openai_url_read_versions(db, source))
        versions.extend(await ensure_web_snapshot_versions(db, source))
    if source.raw_text_path:
        text = ""
        status = V2RecordStatus.READY
        try:
            text = download_text(get_minio_client(), source.raw_text_path)
        except Exception as exc:
            logger.info("Could not read raw text for source version %s: %s", source.id, exc)
            status = V2RecordStatus.FAILED
        kind = SourceRepresentationKind.TRANSCRIPT if source.source_type in {"audio", "video"} else SourceRepresentationKind.EXTRACTED_TEXT
        content_hash = _hash_text(text or source.raw_text_path)
        immutable_text_path = source.raw_text_path
        if text.strip():
            filename = "transcript.txt" if kind == SourceRepresentationKind.TRANSCRIPT else "extracted.txt"
            immutable_text_path = _versioned_object_name(source.id, content_hash, filename)
            upload_text(get_minio_client(), text, immutable_text_path)
        versions.append(
            await ensure_source_version(
                db,
                source,
                kind=kind,
                storage_path=immutable_text_path,
                content_hash=content_hash,
                metadata={
                    "canonical": False,
                    "view": "deterministic_ingestion",
                    "immutable_snapshot": bool(text.strip()),
                    "source_raw_text_path": source.raw_text_path,
                },
                status=status,
            )
        )
        if text.strip():
            page_marked_text = _page_mark_text(text, source)
            page_marked_hash = _hash_text(page_marked_text)
            page_marked_path = _versioned_object_name(source.id, page_marked_hash, "page_marked.txt")
            upload_text(get_minio_client(), page_marked_text, page_marked_path)
            versions.append(
                await ensure_source_version(
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

__all__ = [
    "OpenAIUrlReadVersionDeps",
    "call_openai_url_file_read_with_client",
    "call_openai_url_recovery_with_client",
    "default_openai_url_read_version_deps",
    "ensure_openai_url_read_versions",
    "ensure_source_version",
    "ensure_web_snapshot_versions",
    "fetch_url_for_v2_snapshot",
]
