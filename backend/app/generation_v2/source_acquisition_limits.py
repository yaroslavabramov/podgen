from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.schemas import SourceAcquisitionCandidate
from app.generation_v2.source_acquisition_validation import (
    is_primaryish as _is_primaryish,
    normalize_candidate_url,
    validate_candidate_static,
)
from app.ingestion.web import _check_ssrf
from app.models.source import IngestionStatus, Source


async def validation_rejection(owner, candidate: SourceAcquisitionCandidate) -> tuple[str, str] | None:
    if owner.ingested_count >= max(0, int(settings.generation_v2_source_acquisition_max_ingest_per_round)):
        return "rejected", "Round ingest budget is exhausted."
    reason = validate_candidate_static(candidate, owner.gap_report)
    if reason:
        return "rejected", reason
    try:
        _check_ssrf(candidate.normalized_url)
    except ValueError as exc:
        return "rejected", str(exc)
    await acquire_candidate_url_lock(owner.db, owner.run.project_id, candidate.normalized_url)
    duplicate_source = await existing_source_for_url(
        owner.db,
        owner.run.project_id,
        candidate.normalized_url,
        reclaimed_source_ids=getattr(owner, "reclaimed_stale_source_ids", None),
    )
    if duplicate_source is not None:
        if duplicate_source.is_temporary:
            return "duplicate", "URL acquisition is already in progress."
        return "duplicate", "URL is already in the project corpus."
    if await domain_cap_reached(
        owner.db,
        candidate,
        candidates=owner.candidates,
        project_id=owner.run.project_id,
        generation_run_id=owner.run.id,
    ):
        return "rejected", "Same-domain acquisition cap reached for this round/run."
    return None


async def domain_cap_reached(
    db: AsyncSession,
    candidate: SourceAcquisitionCandidate,
    *,
    candidates: list[SourceAcquisitionCandidate],
    project_id: uuid.UUID,
    generation_run_id: uuid.UUID,
) -> bool:
    if _is_primaryish(candidate):
        return False
    domain = urlparse(candidate.normalized_url).hostname or ""
    if not domain:
        return False
    round_count = sum(
        1
        for item in candidates
        if urlparse(item.normalized_url or item.url).hostname == domain and item.status == "ingested"
    )
    if round_count >= max(1, int(settings.generation_v2_source_acquisition_domain_cap)):
        return True
    result = await db.execute(
        select(Source).where(
            Source.project_id == project_id,
            Source.generation_run_id == generation_run_id,
            Source.original_url.is_not(None),
            Source.is_temporary.is_(False),
            Source.ingestion_status != IngestionStatus.FAILED,
        )
    )
    total = 0
    for source in result.scalars():
        metadata = source.metadata_ or {}
        if not metadata.get("auto_acquired_by_v2"):
            continue
        if urlparse(source.original_url or "").hostname == domain:
            total += 1
    return total >= max(1, int(settings.generation_v2_source_acquisition_domain_cap))


async def existing_source_for_url(
    db: AsyncSession,
    project_id: uuid.UUID,
    normalized_url: str,
    *,
    reclaimed_source_ids: list[uuid.UUID] | None = None,
) -> Source | None:
    result = await db.execute(select(Source).where(Source.project_id == project_id, Source.original_url.is_not(None)))
    for source in result.scalars():
        # Terminal failures are retryable. Active staged rows block concurrent
        # duplicate work until they succeed or fail.
        if source.ingestion_status == IngestionStatus.FAILED:
            continue
        if normalize_candidate_url(source.original_url or "") == normalized_url:
            if source.is_temporary and _staged_source_lease_expired(source):
                source.ingestion_status = IngestionStatus.FAILED
                source.fully_ingested = False
                source.metadata_ = {
                    **(source.metadata_ or {}),
                    "reclaimed_acquisition_claim_token": (source.metadata_ or {}).get("acquisition_claim_token"),
                    "acquisition_claim_token": str(uuid.uuid4()),
                    "vetting_status": "stale_ingest_reclaimed",
                    "ingest_status": "failed",
                    "error": "Acquisition lease expired before ingest completed.",
                }
                if reclaimed_source_ids is not None and source.id not in reclaimed_source_ids:
                    reclaimed_source_ids.append(source.id)
                continue
            return source
    return None


def _staged_source_lease_expired(source: Source) -> bool:
    metadata = source.metadata_ or {}
    raw_expiry = str(metadata.get("acquisition_lease_expires_at") or "").strip()
    expiry: datetime | None = None
    if raw_expiry:
        try:
            expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        except ValueError:
            expiry = None
    if expiry is None:
        changed_at = source.status_changed_at or source.created_at
        if changed_at is None:
            return False
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=timezone.utc)
        expiry = changed_at + timedelta(
            seconds=max(
                30,
                int(settings.generation_v2_source_acquisition_source_timeout_seconds),
            )
            + 60
        )
    elif expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= datetime.now(timezone.utc)


async def acquire_candidate_url_lock(db: AsyncSession, project_id: uuid.UUID, normalized_url: str) -> None:
    """Serialize check+insert for one project URL on PostgreSQL."""
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return
    digest = hashlib.sha256(f"{project_id}:{normalized_url}".encode("utf-8")).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


__all__ = ["acquire_candidate_url_lock", "domain_cap_reached", "existing_source_for_url", "validation_rejection"]
