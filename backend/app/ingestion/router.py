"""Main ingestion router: dispatches extraction by source_type, stores text, chunks, and embeds."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.canonicalizer import canonicalize_sources
from app.ingestion.chunker import structural_chunk
from app.ingestion.normalize import normalize_text
from app.ingestion.pdf import extract_pdf
from app.ingestion.scout import build_content_map
from app.ingestion.video import extract_video
from app.ingestion.web import extract_web
from app.models.source import Source, SourceType
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope
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

    trace_ctx = {
        "component": "ingestion.router",
        "source_id": source_id,
        "project_id": str(source.project_id),
        "source_type": source.source_type,
        "generation_run_id": str(source.generation_run_id) if source.generation_run_id else "",
    }

    with push_trace_context(trace_ctx):
        with tracing_scope(metadata=trace_ctx, tags=["ingestion", "router"]):
            with trace_span(
                "ingestion.run_ingestion",
                run_type="chain",
                inputs={"source_id": source_id, "source_type": source.source_type},
            ) as ingestion_run:
                # --- 1. Extract text -------------------------------------------------------
                text: str
                extracted_title: str | None = None

                with trace_span("ingestion.extract", run_type="tool", inputs={"source_type": source.source_type}):
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
                object_name = f"raw_text/{source.project_id}/{source_id}.md"
                with trace_span(
                    "ingestion.store_raw_text",
                    run_type="tool",
                    inputs={"object_name": object_name, "text_chars": len(text)},
                ):
                    minio = get_minio_client()
                    upload_text(minio, text, object_name)
                    source.raw_text_path = object_name

                # --- 2b. Normalize text (raw preserved in MinIO above) --------------------
                text = normalize_text(text, source.source_type)

                # --- 3. Update source metadata ---------------------------------------------
                if extracted_title and not source.title:
                    source.title = extracted_title[:500]
                source.text_preview = text[:500]
                await db.flush()

                logger.info("Source %s: extracted %d chars, stored at %s", source_id, len(text), object_name)

                # --- 4. Structural chunking ------------------------------------------------
                with trace_span(
                    "ingestion.structural_chunk",
                    run_type="chain",
                    inputs={"source_id": source_id, "text_chars": len(text)},
                ) as chunk_run:
                    chunks = await structural_chunk(text, source_id, db)
                    safe_set_run(chunk_run, outputs={"chunk_count": len(chunks)})

                logger.info("Source %s: created %d chunks", source_id, len(chunks))

                if not chunks:
                    logger.warning("Source %s: no chunks produced, skipping embedding", source_id)
                    safe_set_run(
                        ingestion_run,
                        outputs={"chunk_count": 0, "embedded_point_count": 0, "normalized_text_chars": len(text)},
                    )
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

                with trace_span(
                    "ingestion.embed_chunks",
                    run_type="embedding",
                    inputs={"chunk_count": len(chunk_dicts)},
                ) as embed_run:
                    point_ids = await index_chunks(chunk_dicts)
                    safe_set_run(embed_run, outputs={"embedded_point_count": len(point_ids)})

                # Write qdrant_point_id back to each chunk
                for chunk, point_id in zip(chunks, point_ids):
                    chunk.qdrant_point_id = point_id
                await db.flush()

                # --- 6. Canonicalize source metadata ----------------------------------------
                try:
                    with trace_span("ingestion.canonicalize_sources", run_type="chain"):
                        await canonicalize_sources(str(source.project_id), db)
                    logger.info("Source %s: canonicalization complete", source_id)
                except Exception:
                    logger.warning("Source %s: canonicalization failed, continuing", source_id, exc_info=True)

                # --- 7. Rebuild content map for project ------------------------------------
                try:
                    with trace_span("ingestion.build_content_map", run_type="chain"):
                        await build_content_map(str(source.project_id), db)
                    logger.info("Source %s: content map updated", source_id)
                except Exception:
                    logger.warning("Source %s: content map build failed, continuing", source_id, exc_info=True)

                safe_set_run(
                    ingestion_run,
                    outputs={
                        "normalized_text_chars": len(text),
                        "chunk_count": len(chunks),
                        "embedded_point_count": len(point_ids),
                        "raw_text_path": object_name,
                    },
                )

                logger.info("Source %s: ingestion complete, %d chunks indexed", source_id, len(chunks))
