from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.tools.evidence_utils import (
    _inventory_cautions,
    _neutral_preview,
    _provider_status,
    _readable_version_priority,
    _source_domain,
    _source_length_hint,
    _uuid_or_none,
    _version_readable,
)
from app.ingestion.source_metadata import audited_authority_type
from app.models.generation_v2 import ProviderCorpusFile, RunCorpusView, SourceVersion, V2RecordStatus
from app.models.source import Source


async def build_source_inventory(db: AsyncSession, run_view: RunCorpusView) -> SourceMap:
    """Build neutral, deterministic source inventory for agent-led triage."""
    selected_source_ids = [uuid.UUID(source_id) for source_id in run_view.selected_source_ids if _uuid_or_none(source_id)]
    selected_version_ids = [
        uuid.UUID(version_id)
        for version_id in run_view.selected_source_version_ids
        if _uuid_or_none(version_id)
    ]
    if not selected_source_ids:
        return SourceMap(gaps=["Run corpus view has no selected sources."])
    source_result = await db.execute(select(Source).where(Source.id.in_(selected_source_ids)))
    sources_by_id = {str(source.id): source for source in source_result.scalars()}
    version_result = await db.execute(select(SourceVersion).where(SourceVersion.id.in_(selected_version_ids)))
    versions_by_source: dict[str, list[SourceVersion]] = {}
    for version in version_result.scalars():
        versions_by_source.setdefault(str(version.source_id), []).append(version)
    provider_result = await db.execute(
        select(ProviderCorpusFile).where(ProviderCorpusFile.source_version_id.in_(selected_version_ids))
    )
    provider_by_version = {str(item.source_version_id): item for item in provider_result.scalars()}

    cards: list[SourceCard] = []
    for source_id in run_view.selected_source_ids:
        source = sources_by_id.get(str(source_id))
        if source is None:
            continue
        versions = sorted(
            versions_by_source.get(str(source.id), []),
            key=lambda version: _readable_version_priority(version.kind),
        )
        provider_statuses = [
            provider_by_version[str(version.id)].status
            for version in versions
            if str(version.id) in provider_by_version
        ]
        preview = _neutral_preview(source, versions)
        cards.append(
            SourceCard(
                source_id=str(source.id),
                source_version_ids=[str(version.id) for version in versions],
                evidence_ids=[],
                title=source.title or source.original_url or source.file_path or "Untitled source",
                source_type=source.source_type,
                url=source.original_url,
                domain=_source_domain(source.original_url),
                published_at=source.published_at.isoformat() if source.published_at else None,
                length_hint=_source_length_hint(source, versions),
                provider_status=_provider_status(provider_statuses),
                reliability=source.domain_reliability or "unknown",
                authority=audited_authority_type(source),
                summary=preview,
                useful_for=[],
                gaps_or_cautions=_inventory_cautions(source, versions, provider_statuses),
                representation_ids=[str(version.id) for version in versions],
                representation_summary=", ".join(
                    f"{version.kind}:{version.status}/{version.upload_status}" for version in versions[:8]
                ),
                ready_representation_count=sum(1 for version in versions if version.status == V2RecordStatus.READY),
                failed_representation_count=sum(1 for version in versions if version.status == V2RecordStatus.FAILED),
                coverage_status="ready" if any(_version_readable(version) for version in versions) else "partial",
            )
        )
    return SourceMap(
        source_cards=cards,
        gaps=[] if cards else ["No selected source could be loaded into inventory."],
        retrieval_notes=["Deterministic neutral inventory only; no editorial source-map search has been applied."],
    )
