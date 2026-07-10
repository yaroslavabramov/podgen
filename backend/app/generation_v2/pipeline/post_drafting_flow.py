from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.pipeline.post_drafting_stage import (
    PostDraftingStageContext,
    PostDraftingStageRunner,
    extract_validate_claim_bundle as _extract_validate_claim_bundle,
)
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    QuoteCard,
    ShowBibleContext,
    SourceCard,
    Treatment,
)
from app.models.generation_run import GenerationRun


async def run_post_drafting_until_final(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment: Treatment,
    drafts: list[ActDraft],
    dossiers: list[ActDossier],
    dossier_artifact_ids_by_act: dict[int, str],
    draft_artifact_ids_by_act: dict[int, str],
    treatment_artifact_id: str,
    source_artifact_ids: list[str],
    quote_artifact_ids: list[str],
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    evidence_toolset: Any,
    evidence_tools: list[Any],
    vector_store_id: str | None,
    run_view: Any,
    show_bible: ShowBibleContext,
    generate_audio: bool,
) -> str | None:
    return await PostDraftingStageRunner(
        db,
        run,
        runtime,
        PostDraftingStageContext(
            treatment=treatment,
            drafts=drafts,
            dossiers=dossiers,
            dossier_artifact_ids_by_act=dossier_artifact_ids_by_act,
            draft_artifact_ids_by_act=draft_artifact_ids_by_act,
            treatment_artifact_id=treatment_artifact_id,
            source_artifact_ids=source_artifact_ids,
            quote_artifact_ids=quote_artifact_ids,
            source_cards=source_cards,
            quote_cards=quote_cards,
            evidence_toolset=evidence_toolset,
            evidence_tools=evidence_tools,
            vector_store_id=vector_store_id,
            run_view=run_view,
            show_bible=show_bible,
            generate_audio=generate_audio,
        ),
    ).run_stage()


__all__ = ["_extract_validate_claim_bundle", "run_post_drafting_until_final"]
