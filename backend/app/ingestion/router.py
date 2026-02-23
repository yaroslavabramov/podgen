"""Main ingestion router: dispatches extraction by source_type, stores text, chunks, and embeds."""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ingestion.chunker import structural_chunk
from app.ingestion.pdf import extract_pdf
from app.ingestion.video import extract_video
from app.ingestion.web import extract_web
from app.models.source import Source, SourceType
from app.services.embeddings import index_chunks
from app.services.storage import get_minio_client, upload_text

logger = logging.getLogger(__name__)


async def run_ingestion(source: Source, db: AsyncSession) -> None:
    """Full ingestion pipeline for a single source.

    1. Extract text based on source_type (pdf / web / video / audio).
    2. Store raw text in MinIO.
    3. Update source.text_preview (first 500 chars).
    4. Run structural chunking.
    5. Embed chunks into Qdrant.
    """
    source_id = str(source.id)
    logger.info("Starting ingestion for source %s (type=%s)", source_id, source.source_type)

    # --- 1. Extract text -------------------------------------------------------
    text: str
    extracted_title: str | None = None

    match source.source_type:
        case SourceType.PDF:
            if not source.file_path:
                raise ValueError(f"Source {source_id} is PDF but has no file_path")
            text = await extract_pdf(source.file_path)

        case SourceType.WEB:
            if not source.original_url:
                raise ValueError(f"Source {source_id} is WEB but has no original_url")
            text, extracted_title = await extract_web(source.original_url)

        case SourceType.VIDEO | SourceType.AUDIO:
            text = await extract_video(source)

        case _:
            raise ValueError(f"Unsupported source type: {source.source_type}")

    if not text or not text.strip():
        raise ValueError(f"Extraction returned empty text for source {source_id}")

    # --- 2. Store raw text in MinIO --------------------------------------------
    minio = get_minio_client()
    object_name = f"raw_text/{source.project_id}/{source_id}.md"
    upload_text(minio, text, object_name)
    source.raw_text_path = object_name

    # --- 3. Update source metadata ---------------------------------------------
    if extracted_title and not source.title:
        source.title = extracted_title[:500]
    source.text_preview = text[:500]
    await db.flush()

    logger.info("Source %s: extracted %d chars, stored at %s", source_id, len(text), object_name)

    # --- 4. Structural chunking ------------------------------------------------
    chunks = await structural_chunk(text, source_id, db)
    logger.info("Source %s: created %d chunks", source_id, len(chunks))

    if not chunks:
        logger.warning("Source %s: no chunks produced, skipping embedding", source_id)
        return

    # --- 5. Embed chunks into Qdrant -------------------------------------------
    chunk_dicts = [
        {
            "id": str(chunk.id),
            "content": chunk.content,
            "source_id": source_id,
            "heading_path": chunk.heading_path,
            "source_type": source.source_type,
            "domain_reliability": source.domain_reliability,
        }
        for chunk in chunks
    ]

    point_ids = await index_chunks(chunk_dicts)

    # Write qdrant_point_id back to each chunk
    for chunk, point_id in zip(chunks, point_ids):
        chunk.qdrant_point_id = point_id
    await db.flush()

    logger.info("Source %s: ingestion complete, %d chunks indexed", source_id, len(chunks))
