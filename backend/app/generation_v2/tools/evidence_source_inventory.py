from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.model_context_text import stable_unique_texts
from app.generation_v2.schemas import SourceCard, SourceMap, SourceProfile
from app.generation_v2.source_content_readiness import extract_web_document_text, source_version_model_eligible
from app.generation_v2.source_profiles import ensure_legacy_source_profile, semantic_source_kind
from app.generation_v2.tools.evidence_source_text import source_version_text
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
from app.models.generation_v2 import (
    ProviderCorpusFile,
    RunCorpusView,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)
from app.models.source import Source


async def build_source_inventory(db: AsyncSession, run_view: RunCorpusView) -> SourceMap:
    """Build neutral, deterministic source inventory for agent-led triage."""
    selected_source_ids = [
        uuid.UUID(source_id) for source_id in run_view.selected_source_ids if _uuid_or_none(source_id)
    ]
    selected_version_ids = [
        uuid.UUID(version_id) for version_id in run_view.selected_source_version_ids if _uuid_or_none(version_id)
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
    selected_version_order = {
        str(version_id): index for index, version_id in enumerate(run_view.selected_source_version_ids)
    }

    cards: list[SourceCard] = []
    for source_id in run_view.selected_source_ids:
        source = sources_by_id.get(str(source_id))
        if source is None:
            continue
        versions = sorted(
            versions_by_source.get(str(source.id), []),
            key=lambda version: (
                _readable_version_priority(version.kind),
                selected_version_order.get(str(version.id), len(selected_version_order)),
                str(version.id),
            ),
        )
        provider_statuses = [
            provider_by_version[str(version.id)].status
            for version in versions
            if str(version.id) in provider_by_version
        ]
        preview = _selected_version_preview(versions) or _neutral_preview(source, versions)
        profile_record = await ensure_legacy_source_profile(db, versions[0]) if versions else None
        profile = SourceProfile.model_validate(profile_record.payload) if profile_record is not None else None
        source_kind = semantic_source_kind(source.source_type)
        profile_kind = semantic_source_kind(profile.source_kind if profile is not None else None)
        cards.append(
            SourceCard(
                source_id=str(source.id),
                source_version_ids=[str(version.id) for version in versions],
                evidence_ids=[],
                source_profile_id=str(profile_record.id) if profile_record is not None else None,
                title=source.title or source.original_url or source.file_path or "Untitled source",
                source_type=source_kind if source_kind != "unknown" else profile_kind,
                url=source.original_url,
                domain=_source_domain(source.original_url),
                published_at=source.published_at.isoformat() if source.published_at else None,
                length_hint=_source_length_hint(source, versions),
                provider_status=_provider_status(provider_statuses),
                reliability=source.domain_reliability or "unknown",
                authority=audited_authority_type(source),
                summary=(profile.neutral_summary if profile is not None and profile.neutral_summary else preview),
                useful_for=[],
                gaps_or_cautions=stable_unique_texts(
                    [
                        *_inventory_cautions(source, versions, provider_statuses),
                        *(profile.readability_warnings if profile is not None else []),
                        *(profile.stable_cautions if profile is not None else []),
                    ]
                ),
                representation_ids=[str(version.id) for version in versions],
                representation_summary=", ".join(
                    f"{version.kind}:{version.status}/{version.upload_status}" for version in versions
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


def _selected_version_preview(versions: list[SourceVersion], *, max_chars: int = 900) -> str:
    for version in versions:
        if not source_version_model_eligible(version):
            continue
        text = source_version_text(version)
        if version.kind == SourceRepresentationKind.FETCHED_HTML and text:
            text = extract_web_document_text(text)
        text = text.strip()
        if not text:
            continue
        preview = text[:max_chars]
        omitted = max(0, len(text) - len(preview))
        if omitted:
            return f"{preview}\n\n[Selected-version preview; {omitted} characters omitted.]"
        return preview
    return ""
