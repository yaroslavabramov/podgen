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
from app.generation_v2.source_content_readiness import (
    CONTENT_STATE_CONFIRMED,
    CONTENT_STATE_UNAVAILABLE,
    CONTENT_STATE_UNCONFIRMED,
    SourceContentAssessment,
    assess_media_source_content,
    assess_pdf_source_content,
    assess_web_source_content,
    content_assessment_metadata,
    status_for_content_assessment,
)
from app.generation_v2.source_corpus_helpers import (
    hash_bytes as _hash_bytes,
    hash_text as _hash_text,
    page_mark_text as _page_mark_text,
    versioned_object_name as _versioned_object_name,
)
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion
from app.models.source import Source, SourceType
from app.services.storage import download_file, download_text, get_minio_client, upload_text

logger = logging.getLogger(__name__)


async def ensure_source_versions(db: AsyncSession, source: Source) -> list[SourceVersion]:
    versions: list[SourceVersion] = []
    if source.file_path:
        file_hash = _hash_text(source.file_path)
        file_size = 0
        file_bytes = b""
        file_read_failed = False
        try:
            file_bytes = download_file(get_minio_client(), source.file_path)
            file_hash = _hash_bytes(file_bytes)
            file_size = len(file_bytes)
        except Exception as exc:
            logger.info("Could not read original file for source version %s: %s", source.id, exc)
            file_read_failed = True
        if file_read_failed:
            file_assessment = SourceContentAssessment(
                state=CONTENT_STATE_UNAVAILABLE,
                reasons=("storage_read_failed",),
                char_count=0,
                non_whitespace_chars=0,
                surface=str(SourceRepresentationKind.ORIGINAL_FILE),
            )
        elif str(source.source_type) == str(SourceType.PDF):
            file_assessment = assess_pdf_source_content(
                file_bytes,
                surface=str(SourceRepresentationKind.ORIGINAL_FILE),
                content_type="application/pdf",
            )
        elif str(source.source_type) in {str(SourceType.AUDIO), str(SourceType.VIDEO)}:
            file_assessment = assess_media_source_content(
                file_bytes,
                surface=str(SourceRepresentationKind.ORIGINAL_FILE),
                media_type=str(source.source_type),
            )
        else:
            file_assessment = SourceContentAssessment(
                state=CONTENT_STATE_CONFIRMED if file_bytes else CONTENT_STATE_UNCONFIRMED,
                reasons=() if file_bytes else ("empty_file",),
                char_count=file_size,
                non_whitespace_chars=file_size,
                surface=str(SourceRepresentationKind.ORIGINAL_FILE),
            )
        file_status = status_for_content_assessment(file_assessment)
        versions.append(
            await ensure_source_version(
                db,
                source,
                kind=SourceRepresentationKind.ORIGINAL_FILE,
                storage_path=source.file_path,
                content_hash=file_hash,
                metadata={
                    "canonical": file_assessment.confirmed,
                    "source_type": source.source_type,
                    "original_filename": source.title or Path(source.file_path).name,
                    "native_upload": True,
                    "content_assessment": content_assessment_metadata(file_assessment),
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
        if str(source.source_type) == str(SourceType.WEB):
            versions.extend(await ensure_openai_url_read_versions(db, source))
            versions.extend(await ensure_web_snapshot_versions(db, source))
    if source.raw_text_path:
        text = ""
        read_failed = False
        try:
            text = download_text(get_minio_client(), source.raw_text_path)
        except Exception as exc:
            logger.info("Could not read raw text for source version %s: %s", source.id, exc)
            read_failed = True
        kind = SourceRepresentationKind.TRANSCRIPT if source.source_type in {"audio", "video"} else SourceRepresentationKind.EXTRACTED_TEXT
        if read_failed:
            assessment = SourceContentAssessment(
                state=CONTENT_STATE_UNAVAILABLE,
                reasons=("storage_read_failed",),
                char_count=0,
                non_whitespace_chars=0,
                surface=str(kind),
            )
        elif str(source.source_type) == str(SourceType.WEB):
            assessment = assess_web_source_content(text, surface=str(kind), input_is_markup=False)
        else:
            stripped = text.strip()
            assessment = SourceContentAssessment(
                state=CONTENT_STATE_CONFIRMED if stripped else CONTENT_STATE_UNCONFIRMED,
                reasons=() if stripped else ("empty_text",),
                char_count=len(text),
                non_whitespace_chars=len("".join(text.split())),
                surface=str(kind),
            )
        status = status_for_content_assessment(assessment)
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
                    "source_type": str(source.source_type),
                    "content_assessment": content_assessment_metadata(assessment),
                },
                status=status,
            )
        )
        if text.strip():
            page_marked_text = _page_mark_text(text, source)
            page_marked_assessment = SourceContentAssessment(
                state=assessment.state,
                reasons=assessment.reasons,
                char_count=len(page_marked_text),
                non_whitespace_chars=len("".join(page_marked_text.split())),
                surface=str(SourceRepresentationKind.PAGE_MARKED_TEXT),
            )
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
                    metadata={
                        "canonical": False,
                        "view": "page_marked_companion",
                        "source_type": str(source.source_type),
                        "content_assessment": content_assessment_metadata(page_marked_assessment),
                    },
                    status=status_for_content_assessment(page_marked_assessment),
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
