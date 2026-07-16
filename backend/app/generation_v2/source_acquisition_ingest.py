from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_content_readiness import native_ingest_has_confirmed_provider_content
from app.generation_v2.source_corpus import sync_source_to_project_corpus_now
from app.generation_v2.source_corpus_provider import _provider_uploads_required
from app.generation_v2.source_corpus_versions import ensure_source_versions
from app.generation_v2.source_acquisition_limits import acquire_candidate_url_lock
from app.generation_v2.source_acquisition_validation import normalize_candidate_url
from app.ingestion.router import WebQualityIngestionError, _deactivate_native_ingest_for_web_failure, run_ingestion
from app.models.generation_v2 import SourceVersion
from app.models.source import IngestionStatus, Source


async def native_first_ingest_candidate(db: AsyncSession, source_id: uuid.UUID) -> None:
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError("Source not found")
    claim_token = str((source.metadata_ or {}).get("acquisition_claim_token") or uuid.uuid4())
    source.metadata_ = {**(source.metadata_ or {}), "acquisition_claim_token": claim_token}
    # Materialize native representations while the source is still staged. Do
    # not attach it to the project provider corpus until ingest has succeeded.
    await ensure_source_versions(db, source)
    await db.commit()
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError("Source not found after V2 native ingest")
    try:
        source.ingestion_status = IngestionStatus.PROCESSING
        await db.commit()
        # Promotion requires a real retrieval surface. If local indexing fails,
        # only the provider-backed fallback below may still make the source ready.
        await run_ingestion(source, db, require_indexed_chunks=True)
        source.ingestion_status = IngestionStatus.COMPLETED
        source.fully_ingested = True
        metadata = dict(source.metadata_ or {})
        metadata.pop("error", None)
        metadata["ingest_status"] = "completed"
        source.metadata_ = metadata
        await db.commit()
    except Exception as exc:
        await db.rollback()
        source = await db.get(Source, source_id)
        if source is None:
            raise
        if isinstance(exc, WebQualityIngestionError) or getattr(exc, "invalidate_native_ingest", False):
            await _deactivate_native_ingest_for_web_failure(db, source, str(exc))
            metadata = dict(source.metadata_ or {})
            metadata.update(getattr(exc, "metadata_patch", lambda: {})())
            metadata["error"] = str(exc)
            metadata["deterministic_ingest_error"] = str(exc)
            metadata["ingest_status"] = "failed_invalid_web_source"
            source.metadata_ = metadata
            source.ingestion_status = IngestionStatus.FAILED
            source.fully_ingested = False
            await db.commit()
            raise
        if native_ready(source.metadata_) and _provider_uploads_required():
            metadata = dict(source.metadata_ or {})
            metadata.pop("error", None)
            metadata["deterministic_ingest_error"] = str(exc)
            metadata["ingest_status"] = "completed_v2_native_parser_failed"
            source.metadata_ = metadata
            source.ingestion_status = IngestionStatus.COMPLETED
            source.fully_ingested = False
            await db.commit()
        else:
            source.ingestion_status = IngestionStatus.FAILED
            source.fully_ingested = False
            source.metadata_ = {**(source.metadata_ or {}), "error": str(exc), "ingest_status": "failed"}
            await db.commit()
            raise

    # Only successfully ingested sources are attached to the shared provider
    # corpus and promoted into the visible project source list.
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError("Source not found before acquisition promotion")
    await _assert_acquisition_promotion_winner(db, source, claim_token=claim_token)
    promotion_summary = await sync_source_to_project_corpus_now(db, source)
    if not promotion_summary.get("canonical_source_version_id"):
        raise RuntimeError("Acquired source has no ready canonical source version.")
    if _provider_uploads_required() and not (
        promotion_summary.get("provider_vector_store_id")
        and int(promotion_summary.get("ready_provider_file_count") or 0) > 0
    ):
        raise RuntimeError("Acquired source did not become ready in the provider corpus.")
    await _assert_acquisition_promotion_winner(db, source, claim_token=claim_token)
    source.is_temporary = False
    source.metadata_ = {
        **(source.metadata_ or {}),
        "vetting_status": "accepted",
        "promoted_after_ingest": True,
    }
    await db.commit()


async def _assert_acquisition_promotion_winner(
    db: AsyncSession,
    source: Source,
    *,
    claim_token: str,
) -> None:
    """Serialize final promotion with retries that may have reclaimed this URL."""
    normalized_url = normalize_candidate_url(source.original_url or "")
    if not normalized_url:
        raise RuntimeError("Acquired source has no valid URL at promotion time.")
    await acquire_candidate_url_lock(db, source.project_id, normalized_url)
    result = await db.execute(
        select(Source).where(
            Source.project_id == source.project_id,
            Source.original_url.is_not(None),
        ).execution_options(populate_existing=True)
    )
    current_source: Source | None = None
    for candidate in result.scalars():
        if candidate.id == source.id:
            current_source = candidate
            continue
        if candidate.ingestion_status == IngestionStatus.FAILED:
            continue
        if normalize_candidate_url(candidate.original_url or "") != normalized_url:
            continue
        candidate_metadata = candidate.metadata_ or {}
        if not candidate.is_temporary or not candidate_metadata.get("auto_acquired_by_v2"):
            raise RuntimeError("Acquisition promotion is blocked by an existing project source for the same URL.")
        if _acquisition_attempt_order(candidate) > _acquisition_attempt_order(source):
            raise RuntimeError("Acquisition was superseded by a newer attempt for the same URL.")
    if current_source is None:
        raise RuntimeError("Acquisition source disappeared before promotion.")
    current_metadata = current_source.metadata_ or {}
    if (
        current_source.ingestion_status == IngestionStatus.FAILED
        or current_metadata.get("vetting_status") == "stale_ingest_reclaimed"
        or str(current_metadata.get("acquisition_claim_token") or "") != claim_token
    ):
        raise RuntimeError("Acquisition attempt was reclaimed before promotion.")


def _acquisition_attempt_order(source: Source) -> tuple[float, float, str]:
    metadata = source.metadata_ or {}
    return (
        _datetime_order_value(source.created_at),
        _datetime_order_value(metadata.get("acquisition_lease_expires_at")),
        str(source.id),
    )


def _datetime_order_value(value: Any) -> float:
    parsed = value if isinstance(value, datetime) else None
    if parsed is None and isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def native_ready(metadata: dict | None) -> bool:
    return native_ingest_has_confirmed_provider_content(metadata)


async def source_versions(db: AsyncSession, source_id: uuid.UUID) -> list[SourceVersion]:
    result = await db.execute(select(SourceVersion).where(SourceVersion.source_id == source_id).order_by(SourceVersion.created_at.asc()))
    return list(result.scalars())
