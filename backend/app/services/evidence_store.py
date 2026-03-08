"""Shared canonical evidence storage and provenance helpers."""

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evidence import (
    AssetType,
    EvidenceProvenance,
    EvidenceQuestionRelevance,
    EvidenceRetrievedBy,
    EvidenceSpan,
)
from app.models.source import Source
from app.pipeline.phase3_research.normalization import normalize_research_text


@dataclass(frozen=True)
class EvidenceProvenanceContext:
    generation_run_id: str
    manifest_id: str | None
    question_id: str
    branch_id: str
    asset_type: str
    retrieved_by: str
    query_text: str | None = None
    retrieval_score: float | None = None
    direct_or_contextual: str | None = None


def canonical_source_id(source: Source) -> uuid.UUID:
    return source.is_duplicate_of or source.id


def normalize_locator(locator: dict | None) -> str | None:
    if locator is None:
        return None
    return normalize_research_text(
        json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _coerce_asset_type(value: str | AssetType) -> str:
    if isinstance(value, AssetType):
        return value.value
    normalized = str(value).strip().lower()
    return AssetType(normalized).value


def normalize_direct_or_contextual(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    try:
        return EvidenceQuestionRelevance(normalized).value
    except ValueError:
        return None


async def _load_source(db: AsyncSession, source_id: str | uuid.UUID) -> Source:
    source_uuid = source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id))
    result = await db.execute(select(Source).where(Source.id == source_uuid))
    source = result.scalar_one()
    return source


async def _candidate_source_ids(db: AsyncSession, source: Source) -> list[uuid.UUID]:
    canonical_id = canonical_source_id(source)
    result = await db.execute(
        select(Source.id).where(
            or_(
                Source.id == canonical_id,
                Source.is_duplicate_of == canonical_id,
            )
        )
    )
    return [row[0] for row in result.all()]


async def find_existing_evidence(
    *,
    db: AsyncSession,
    project_id: str,
    source_id: str | uuid.UUID,
    verbatim_quote: str,
    locator: dict | None,
) -> EvidenceSpan | None:
    source = await _load_source(db, source_id)
    candidate_ids = await _candidate_source_ids(db, source)
    if not candidate_ids:
        return None

    target_quote = normalize_research_text(verbatim_quote)
    target_locator = normalize_locator(locator)
    result = await db.execute(
        select(EvidenceSpan).where(
            EvidenceSpan.project_id == uuid.UUID(project_id),
            EvidenceSpan.source_id.in_(candidate_ids),
        )
    )
    candidates = result.scalars().all()
    for evidence in candidates:
        if normalize_research_text(evidence.verbatim_quote) != target_quote:
            continue
        if normalize_locator(evidence.locator) == target_locator:
            return evidence
        if target_locator is None and evidence.locator is None:
            return evidence
    return None


async def get_or_create_canonical_evidence(
    *,
    db: AsyncSession,
    project_id: str,
    source_id: str | uuid.UUID,
    verbatim_quote: str,
    asset_type: str,
    locator: dict | None = None,
    chunk_id: str | uuid.UUID | None = None,
    narrative_value_score: float | None = None,
    tellability_notes: str | None = None,
    reliability_hint: str | None = None,
    start_idx: int | None = None,
    end_idx: int | None = None,
) -> tuple[EvidenceSpan, bool]:
    normalized_asset_type = _coerce_asset_type(asset_type)
    existing = await find_existing_evidence(
        db=db,
        project_id=project_id,
        source_id=source_id,
        verbatim_quote=verbatim_quote,
        locator=locator,
    )
    if existing is not None:
        if existing.chunk_id is None and chunk_id:
            existing.chunk_id = chunk_id if isinstance(chunk_id, uuid.UUID) else uuid.UUID(str(chunk_id))
        if existing.locator is None and locator is not None:
            existing.locator = locator
        if existing.reliability_hint is None and reliability_hint:
            existing.reliability_hint = reliability_hint
        if narrative_value_score is not None:
            current = existing.narrative_value_score or 0.0
            existing.narrative_value_score = max(current, narrative_value_score)
        if not existing.tellability_notes and tellability_notes:
            existing.tellability_notes = tellability_notes
        if existing.start_idx is None and start_idx is not None:
            existing.start_idx = start_idx
        if existing.end_idx is None and end_idx is not None:
            existing.end_idx = end_idx
        return existing, False

    evidence = EvidenceSpan(
        id=uuid.uuid4(),
        project_id=uuid.UUID(project_id),
        source_id=source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id)),
        chunk_id=chunk_id if isinstance(chunk_id, uuid.UUID) or chunk_id is None else uuid.UUID(str(chunk_id)),
        verbatim_quote=verbatim_quote,
        asset_type=normalized_asset_type,
        locator=locator,
        reliability_hint=reliability_hint,
        narrative_value_score=narrative_value_score,
        tellability_notes=tellability_notes,
        start_idx=start_idx,
        end_idx=end_idx,
    )
    db.add(evidence)
    await db.flush()
    return evidence, True


async def ensure_evidence_provenance(
    *,
    db: AsyncSession,
    evidence_id: str | uuid.UUID,
    context: EvidenceProvenanceContext,
) -> EvidenceProvenance:
    evidence_uuid = evidence_id if isinstance(evidence_id, uuid.UUID) else uuid.UUID(str(evidence_id))
    generation_run_uuid = uuid.UUID(context.generation_run_id)
    manifest_uuid = uuid.UUID(context.manifest_id) if context.manifest_id else None
    retrieved_by = EvidenceRetrievedBy(context.retrieved_by)
    asset_type = _coerce_asset_type(context.asset_type)
    direct_or_contextual = normalize_direct_or_contextual(context.direct_or_contextual)

    result = await db.execute(
        select(EvidenceProvenance).where(
            EvidenceProvenance.evidence_id == evidence_uuid,
            EvidenceProvenance.generation_run_id == generation_run_uuid,
            EvidenceProvenance.question_id == context.question_id,
            EvidenceProvenance.branch_id == context.branch_id,
            EvidenceProvenance.asset_type == asset_type,
            EvidenceProvenance.retrieved_by == retrieved_by,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.query_text is None and context.query_text:
            existing.query_text = context.query_text
        if existing.retrieval_score is None and context.retrieval_score is not None:
            existing.retrieval_score = context.retrieval_score
        if existing.manifest_id is None and manifest_uuid is not None:
            existing.manifest_id = manifest_uuid
        if direct_or_contextual:
            current = normalize_direct_or_contextual(existing.direct_or_contextual)
            if current is None or (current == EvidenceQuestionRelevance.CONTEXTUAL and direct_or_contextual == EvidenceQuestionRelevance.DIRECT):
                existing.direct_or_contextual = direct_or_contextual
        return existing

    provenance = EvidenceProvenance(
        id=uuid.uuid4(),
        evidence_id=evidence_uuid,
        generation_run_id=generation_run_uuid,
        manifest_id=manifest_uuid,
        question_id=context.question_id,
        branch_id=context.branch_id,
        asset_type=asset_type,
        retrieved_by=retrieved_by,
        query_text=context.query_text,
        retrieval_score=context.retrieval_score,
        direct_or_contextual=direct_or_contextual,
    )
    db.add(provenance)
    await db.flush()
    return provenance
