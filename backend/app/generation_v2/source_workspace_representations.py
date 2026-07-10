from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_workspace_web_representations import (
    capture_rendered_snapshot,
    capture_web_representations,
)
from app.generation_v2.source_workspace_provider import (
    _maybe_upload_file_representation,
    _maybe_upload_text_representation,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import SourceRepresentation, SourceRepresentationKind, V2RecordStatus
from app.models.source import Source
from app.services.storage import download_text, get_minio_client

logger = logging.getLogger(__name__)

async def register_source_representations(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    vector_store_id: str | None,
) -> list[uuid.UUID]:
    representation_ids: list[uuid.UUID] = []

    if source.file_path:
        rep = await save_representation(
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
        rep = await save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.ORIGINAL_URL,
            storage_path=source.original_url,
            status=V2RecordStatus.READY,
            metadata={"canonical": True},
        )
        representation_ids.append(rep.id)
        representation_ids.extend(await capture_web_representations(db, source))

    if source.raw_text_path:
        try:
            text = download_text(get_minio_client(), source.raw_text_path)
            rep = await save_representation(
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
            rep = await save_representation(
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


async def save_representation(
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
    content_hash = hash_bytes(content) if content is not None else None
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


def hash_bytes(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()

__all__ = [
    "capture_rendered_snapshot",
    "capture_web_representations",
]
