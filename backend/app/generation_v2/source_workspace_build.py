from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import QuoteCard, SourceCard
from app.generation_v2.source_workspace_cards import (
    compact_preview as _compact_preview,
    infer_cautions as _infer_cautions,
    infer_useful_for as _infer_useful_for,
    quote_cards_from_chunks as _quote_cards_from_chunks,
)
from app.generation_v2.source_workspace_helpers import (
    load_source_chunks as _load_source_chunks,
    ready_text_representation_id as _ready_text_representation_id,
    representation_counts as _representation_counts,
    source_preview as _source_preview,
)
from app.generation_v2.source_workspace_representations import (
    register_source_representations as _register_source_representations,
)
from app.models.generation_run import GenerationRun
from app.models.source import Source


@dataclass
class SourceWorkspaceCardsResult:
    source_cards: list[SourceCard] = field(default_factory=list)
    quote_cards: list[QuoteCard] = field(default_factory=list)
    ready_source_count: int = 0
    failed_source_count: int = 0
    ready_representation_count: int = 0
    failed_representation_count: int = 0
    workspace_warnings: list[str] = field(default_factory=list)


async def build_source_workspace_cards(
    db: AsyncSession,
    run: GenerationRun,
    *,
    sources: list[Source],
    vector_store_id: str | None,
) -> SourceWorkspaceCardsResult:
    result = SourceWorkspaceCardsResult()
    for source in sources:
        representation_ids = await _register_source_representations(db, run, source, vector_store_id)
        ready_count, failed_count, coverage_status = await _representation_counts(db, representation_ids)
        result.ready_representation_count += ready_count
        result.failed_representation_count += failed_count
        if ready_count:
            result.ready_source_count += 1
        else:
            result.failed_source_count += 1
        chunks = await _load_source_chunks(db, source.id)
        extracted_preview = await _source_preview(source, chunks)
        cautions = _infer_cautions(source, extracted_preview)
        if not extracted_preview.strip() and ready_count:
            result.workspace_warnings.append(
                f"{source.title or source.id}: deterministic text missing; native/provider views available."
            )
        source_card = SourceCard(
            source_id=str(source.id),
            title=source.title or source.original_url or source.file_path or "Untitled source",
            source_type=source.source_type,
            url=source.original_url,
            reliability=source.domain_reliability or "unknown",
            authority=source.authority_type or "unknown",
            summary=_compact_preview(extracted_preview, 900),
            useful_for=_infer_useful_for(extracted_preview),
            gaps_or_cautions=cautions,
            representation_ids=[str(value) for value in representation_ids],
            representation_summary=f"{ready_count} ready, {failed_count} failed representations",
            ready_representation_count=ready_count,
            failed_representation_count=failed_count,
            coverage_status=coverage_status,
        )
        result.source_cards.append(source_card)
        quote_representation_id = await _ready_text_representation_id(db, source.id)
        result.quote_cards.extend(_quote_cards_from_chunks(source, chunks, representation_id=quote_representation_id))
    return result


__all__ = ["SourceWorkspaceCardsResult", "build_source_workspace_cards"]
