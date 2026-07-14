"""Main ingestion router: dispatches extraction by source_type, stores text, chunks, and embeds."""

import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ingestion.canonicalizer import canonicalize_source
from app.ingestion.chunker import structural_chunk
from app.ingestion.normalize import normalize_text
from app.ingestion.pdf import extract_pdf
from app.ingestion.video import extract_video
from app.ingestion.web import extract_web_with_profile
from app.ingestion.web_quality import (
    assess_web_fetch_health,
    clean_web_text,
    resolve_web_title_detailed,
)
from app.models.project import Project
from app.models.source import Source, SourceType
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope
from app.services.embeddings import index_chunks
from app.services.storage import get_minio_client, upload_text

logger = logging.getLogger(__name__)


class WebQualityIngestionError(ValueError):
    """Non-retryable web extraction failure that invalidates native snapshots."""

    invalidate_native_ingest = True

    def __init__(
        self,
        message: str,
        *,
        state: str,
        reasons: list[str],
        blocked: bool,
        extraction_profile: str | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.reasons = reasons
        self.blocked = blocked
        self.extraction_profile = extraction_profile

    def metadata_patch(self) -> dict:
        patch: dict = {
            "web_quality": {
                "state": self.state,
                "reasons": self.reasons,
                "blocked": self.blocked,
            }
        }
        if self.extraction_profile:
            patch["web_extraction_profile"] = self.extraction_profile
        return patch


class IngestionIndexingError(RuntimeError):
    """The extracted source did not become searchable in the local index."""


def _validated_source_title_for_persistence(value: str) -> str:
    title = str(value)
    if len(title) > 500:
        raise ValueError(
            "semantic_field_too_long: field=source.title "
            f"max_length=500 actual_length={len(title)}"
        )
    return title


async def _deactivate_native_ingest_for_web_failure(
    db: AsyncSession,
    source: Source,
    reason: str,
) -> None:
    """Remove invalid web snapshots from the active V2 corpus surface."""
    try:
        from app.generation_v2.source_corpus import mark_provider_files_inactive
        from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus

        await db.execute(
            update(SourceVersion)
            .where(
                SourceVersion.source_id == source.id,
                SourceVersion.kind != str(SourceRepresentationKind.ORIGINAL_URL),
            )
            .values(status=V2RecordStatus.FAILED, is_active=False)
        )
        await mark_provider_files_inactive(
            db,
            source_id=source.id,
            cleanup_status="invalid_ingest_output",
        )
        source.metadata_ = {
            **(source.metadata_ or {}),
            "v2_native_ingest": {
                "status": "failed",
                "error": reason,
                "synced_at": datetime.utcnow().isoformat(),
            },
        }
        await db.flush()
    except Exception:
        logger.warning("Failed to deactivate invalid V2 native ingest for source %s", source.id, exc_info=True)


async def run_ingestion(
    source: Source,
    db: AsyncSession,
    *,
    require_indexed_chunks: bool = False,
) -> None:
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
                extracted_date: str | None = None
                web_extraction_profile: str | None = None
                web_quality_state: str | None = None
                web_quality_reasons: list[str] = []
                blocked_web_source = False

                with trace_span(
                    "ingestion.extract",
                    run_type="tool",
                    inputs={"source_type": source.source_type},
                ) as extract_run:
                    match source.source_type:
                        case SourceType.PDF:
                            if not source.file_path:
                                raise ValueError(f"Source {source_id} is PDF but has no file_path")
                            text = await extract_pdf(source.file_path)

                        case SourceType.WEB:
                            if not source.original_url:
                                raise ValueError(f"Source {source_id} is WEB but has no original_url")
                            (
                                text,
                                extracted_title,
                                extracted_date,
                                web_extraction_profile,
                            ) = await extract_web_with_profile(source.original_url)

                        case SourceType.VIDEO | SourceType.AUDIO:
                            # Load project language for Deepgram transcription
                            from sqlalchemy import select as sa_select
                            proj_result = await db.execute(
                                sa_select(Project.language).where(Project.id == source.project_id)
                            )
                            project_lang = proj_result.scalar_one_or_none()
                            text = await extract_video(source, language=project_lang)

                        case _:
                            raise ValueError(f"Unsupported source type: {source.source_type}")
                    safe_set_run(
                        extract_run,
                        outputs={
                            "text_chars": len(text) if text else 0,
                            "extraction_profile": web_extraction_profile or source.source_type,
                            "extracted_title_present": bool(extracted_title),
                            "extracted_date_present": bool(extracted_date),
                        },
                    )

                if not text or not text.strip():
                    raise ValueError(f"Extraction returned empty text for source {source_id}")

                # --- 1a. Gate A: fetch health / anti-bot detection -----------------------
                if source.source_type == SourceType.WEB:
                    with trace_span(
                        "ingestion.web_quality.gate_a",
                        run_type="tool",
                        inputs={
                            "source_id": source_id,
                            "input_chars": len(text),
                            "min_chars": settings.web_quality_gate_a_min_chars,
                        },
                    ) as gate_run:
                        assessment = assess_web_fetch_health(
                            text,
                            min_chars=settings.web_quality_gate_a_min_chars,
                        )
                        web_quality_state = assessment.state
                        web_quality_reasons = assessment.reasons
                        blocked_web_source = assessment.state == "blocked"
                        safe_set_run(
                            gate_run,
                            outputs={
                                "state": assessment.state,
                                "reasons": assessment.reasons,
                                "char_count": assessment.char_count,
                                "non_whitespace_chars": assessment.non_whitespace_chars,
                                "blocked": blocked_web_source,
                            },
                        )

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
                text = normalize_text(
                    text,
                    source.source_type,
                    extraction_profile=web_extraction_profile,
                )

                # --- 2c. Gate B: web-specific cleanup ------------------------------------
                if source.source_type == SourceType.WEB:
                    with trace_span(
                        "ingestion.web_quality.gate_b",
                        run_type="tool",
                        inputs={"source_id": source_id, "input_chars": len(text), "state": web_quality_state or "unknown"},
                    ) as gate_run:
                        cleaned = clean_web_text(text)
                        clean_delta = len(text) - len(cleaned)
                        text = cleaned
                        safe_set_run(
                            gate_run,
                            outputs={
                                "output_chars": len(cleaned),
                                "removed_chars": clean_delta if clean_delta > 0 else 0,
                            },
                        )
                # --- 3. Update source metadata ---------------------------------------------
                if source.source_type == SourceType.WEB:
                    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
                    metadata = dict(metadata)
                    metadata["web_quality"] = {
                        "state": web_quality_state or "ok",
                        "reasons": web_quality_reasons,
                        "blocked": blocked_web_source,
                    }
                    if web_extraction_profile:
                        metadata["web_extraction_profile"] = web_extraction_profile
                    source.metadata_ = metadata

                title_debug_meta: dict[str, int | str] = {}
                if source.source_type == SourceType.WEB:
                    resolved_title, title_debug_meta = resolve_web_title_detailed(
                        extracted_title,
                        text=text,
                        url=source.original_url,
                    )
                else:
                    resolved_title = extracted_title
                if resolved_title and not source.title:
                    source.title = _validated_source_title_for_persistence(resolved_title)
                if source.source_type == SourceType.WEB:
                    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
                    metadata = dict(metadata)
                    if title_debug_meta:
                        metadata["title_resolution"] = title_debug_meta
                    source.metadata_ = metadata
                source.text_preview = text[:500]

                # Use trafilatura-extracted date if available (skips LLM date extraction)
                if source.source_type == SourceType.WEB and extracted_date and not source.published_at:
                    from datetime import datetime
                    try:
                        source.published_at = datetime.strptime(extracted_date, "%Y-%m-%d")
                    except ValueError:
                        logger.debug("Invalid trafilatura date format: %s", extracted_date)
                await db.flush()

                if source.source_type == SourceType.WEB and blocked_web_source:
                    reasons = ", ".join(web_quality_reasons) if web_quality_reasons else "unknown"
                    message = f"Web source blocked by anti-bot/security wall (reasons: {reasons})"
                    raise WebQualityIngestionError(
                        message,
                        state=web_quality_state or "blocked",
                        reasons=web_quality_reasons,
                        blocked=True,
                        extraction_profile=web_extraction_profile,
                    )
                if source.source_type == SourceType.WEB and web_quality_state == "low_signal":
                    reasons = ", ".join(web_quality_reasons) if web_quality_reasons else "unknown"
                    message = f"Web source extraction returned low-signal text (reasons: {reasons})"
                    raise WebQualityIngestionError(
                        message,
                        state="low_signal",
                        reasons=web_quality_reasons,
                        blocked=False,
                        extraction_profile=web_extraction_profile,
                    )

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
                    if require_indexed_chunks:
                        raise IngestionIndexingError(
                            f"Ingestion produced no searchable chunks for source {source_id}"
                        )
                    logger.warning("Source %s: no chunks produced, skipping embedding", source_id)
                    try:
                        with trace_span("ingestion.canonicalize_source", run_type="chain"):
                            await canonicalize_source(source.id, db)
                        logger.info("Source %s: source-scoped canonicalization complete", source_id)
                    except Exception:
                        logger.warning("Source %s: canonicalization failed, continuing", source_id, exc_info=True)

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

                if require_indexed_chunks and len(point_ids) != len(chunks):
                    raise IngestionIndexingError(
                        f"Ingestion indexed {len(point_ids)} of {len(chunks)} chunks for source {source_id}"
                    )

                # Write qdrant_point_id back to each chunk
                for chunk, point_id in zip(chunks, point_ids):
                    chunk.qdrant_point_id = point_id
                await db.flush()

                # --- 6. Canonicalize source metadata ----------------------------------------
                try:
                    with trace_span("ingestion.canonicalize_source", run_type="chain"):
                        await canonicalize_source(source.id, db)
                    logger.info("Source %s: source-scoped canonicalization complete", source_id)
                except Exception:
                    logger.warning("Source %s: canonicalization failed, continuing", source_id, exc_info=True)

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
