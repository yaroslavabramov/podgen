from __future__ import annotations

import uuid
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.schemas import SourceAcquisitionCandidate
from app.generation_v2.source_acquisition_validation import (
    is_primaryish as _is_primaryish,
    normalize_candidate_url,
    validate_candidate_static,
)
from app.models.source import Source


async def validation_rejection(owner, candidate: SourceAcquisitionCandidate) -> tuple[str, str] | None:
    if owner.ingested_count >= max(0, int(settings.generation_v2_source_acquisition_max_ingest_per_round)):
        return "rejected", "Round ingest budget is exhausted."
    reason = validate_candidate_static(candidate, owner.gap_report)
    if reason:
        return "rejected", reason
    duplicate_source = await existing_source_for_url(owner.db, owner.run.project_id, candidate.normalized_url)
    if duplicate_source is not None:
        return "duplicate", f"URL is already in project corpus as source_id {duplicate_source.id}."
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


async def existing_source_for_url(db: AsyncSession, project_id: uuid.UUID, normalized_url: str) -> Source | None:
    result = await db.execute(select(Source).where(Source.project_id == project_id, Source.original_url.is_not(None)))
    for source in result.scalars():
        if normalize_candidate_url(source.original_url or "") == normalized_url:
            return source
    return None


__all__ = ["domain_cap_reached", "existing_source_for_url", "validation_rejection"]
