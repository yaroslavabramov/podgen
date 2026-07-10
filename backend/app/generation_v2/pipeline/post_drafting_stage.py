from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import critique_episode, extract_and_link_claims
from app.generation_v2.contracts.pipeline import V2Phase, V2PipelineStep
from app.generation_v2.pipeline.artifact_persistence import (
    save_episode_critique_artifact,
    validate_and_save_claim_bundle,
)
from app.generation_v2.pipeline.critique_rewrite_flow import rewrite_drafts_for_critique
from app.generation_v2.pipeline.finalization_flow import finalize_generation_outputs
from app.generation_v2.pipeline.status import commit_step, start_phase
from app.generation_v2.pipeline.validation import apply_forbidden_phrase_gate, blocking_fact_issues
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    QuoteCard,
    ShowBibleContext,
    SourceCard,
    Treatment,
)
from app.generation_v2.source_corpus import list_evidence_dicts
from app.models.generation_run import GenerationRun


@dataclass
class PostDraftingStageContext:
    treatment: Treatment
    drafts: list[ActDraft]
    dossiers: list[ActDossier]
    dossier_artifact_ids_by_act: dict[int, str]
    draft_artifact_ids_by_act: dict[int, str]
    treatment_artifact_id: str
    source_artifact_ids: list[str]
    quote_artifact_ids: list[str]
    source_cards: list[SourceCard]
    quote_cards: list[QuoteCard]
    evidence_toolset: Any
    evidence_tools: list[Any]
    vector_store_id: str | None
    run_view: Any
    show_bible: ShowBibleContext
    generate_audio: bool


class PostDraftingStageRunner:
    def __init__(self, db: AsyncSession, run: GenerationRun, runtime: V2Runtime, context: PostDraftingStageContext) -> None:
        self.db = db
        self.run = run
        self.runtime = runtime
        self.context = context

    async def run_stage(self) -> str | None:
        critique, critique_artifact_id = await self.run_critique()
        await commit_step(self.run, self.db, V2PipelineStep.CRITIQUE, 78, next_step=V2PipelineStep.FACT_CITATION)

        bundle, bundle_artifact_ids, validation_report, validation_artifact_id = await self.extract_validate_claim_bundle(
            critique=critique,
            critique_artifact_id=critique_artifact_id,
            pass_key="initial",
        )

        if bundle.fact_issues:
            bundle, bundle_artifact_ids, validation_report, validation_artifact_id = await self.repair_fact_issues(
                critique=critique,
                critique_artifact_id=critique_artifact_id,
                bundle_artifact_ids=bundle_artifact_ids,
                fact_issues=bundle.fact_issues,
            )

        blocking_issues = blocking_fact_issues(bundle.fact_issues)
        if blocking_issues:
            await self.db.commit()
            raise RuntimeError(f"V2 fact gate failed with {len(blocking_issues)} blocking issue(s)")
        return await finalize_generation_outputs(
            self.db,
            self.run,
            self.runtime,
            treatment=self.context.treatment,
            drafts=self.context.drafts,
            bundle=bundle,
            source_cards=self.context.source_cards,
            treatment_artifact_id=self.context.treatment_artifact_id,
            draft_artifact_ids_by_act=self.context.draft_artifact_ids_by_act,
            bundle_artifact_ids=bundle_artifact_ids,
            validation_artifact_id=validation_artifact_id,
            validation_report=validation_report,
            generate_audio=self.context.generate_audio,
        )

    async def run_critique(self) -> tuple[Any, str]:
        await start_phase(self.run, self.db, V2Phase.CRITIQUE_REWRITE, "Critiquing complete draft", 72)
        critique = await critique_episode(
            self.db,
            self.run,
            self.runtime,
            treatment=self.context.treatment,
            source_cards=self.context.source_cards,
            quote_cards=self.context.quote_cards,
            drafts=self.context.drafts,
            evidence_tools=self.context.evidence_tools,
            show_bible=self.context.show_bible,
        )
        critique = apply_forbidden_phrase_gate(critique, self.context.drafts, self.context.show_bible)
        critique_artifact_id = await save_episode_critique_artifact(
            self.db,
            self.run,
            self.runtime,
            critique,
            treatment_artifact_id=self.context.treatment_artifact_id,
            draft_artifact_ids_by_act=self.context.draft_artifact_ids_by_act,
            source_artifact_ids=self.context.source_artifact_ids,
            quote_artifact_ids=self.context.quote_artifact_ids,
            has_vector_store=bool(self.context.vector_store_id),
        )
        if not critique.passed and critique.rewrite_instructions:
            await self.rewrite_for_critique(critique, critique_artifact_id)
        return critique, critique_artifact_id

    async def rewrite_for_critique(
        self,
        critique: Any,
        critique_artifact_id: str,
        *,
        fact_issues: list[Any] | None = None,
        bundle_artifact_ids: dict[str, str] | None = None,
    ) -> None:
        revision = await rewrite_drafts_for_critique(
            self.db,
            self.run,
            self.runtime,
            treatment=self.context.treatment,
            dossiers=self.context.dossiers,
            drafts=self.context.drafts,
            critique=critique,
            critique_artifact_id=critique_artifact_id,
            treatment_artifact_id=self.context.treatment_artifact_id,
            draft_artifact_ids_by_act=self.context.draft_artifact_ids_by_act,
            dossier_artifact_ids_by_act=self.context.dossier_artifact_ids_by_act,
            evidence_tools=self.context.evidence_tools,
            show_bible=self.context.show_bible,
            has_vector_store=bool(self.context.vector_store_id),
            fact_issues=fact_issues,
            bundle_artifact_ids=bundle_artifact_ids,
        )
        self.context.drafts = revision.drafts
        self.context.draft_artifact_ids_by_act = revision.draft_artifact_ids_by_act

    async def extract_validate_claim_bundle(
        self,
        *,
        critique: Any,
        critique_artifact_id: str,
        pass_key: str,
    ) -> Any:
        return await extract_validate_claim_bundle(
            self.db,
            self.run,
            self.runtime,
            source_cards=self.context.source_cards,
            quote_cards=self.context.quote_cards,
            evidence_toolset=self.context.evidence_toolset,
            evidence_tools=self.context.evidence_tools,
            dossiers=self.context.dossiers,
            drafts=self.context.drafts,
            critique=critique,
            critique_artifact_id=critique_artifact_id,
            draft_artifact_ids_by_act=self.context.draft_artifact_ids_by_act,
            source_artifact_ids=self.context.source_artifact_ids,
            quote_artifact_ids=self.context.quote_artifact_ids,
            vector_store_id=self.context.vector_store_id,
            run_view=self.context.run_view,
            show_bible=self.context.show_bible,
            pass_key=pass_key,
        )

    async def repair_fact_issues(
        self,
        *,
        critique: Any,
        critique_artifact_id: str,
        bundle_artifact_ids: dict[str, str],
        fact_issues: list[Any],
    ) -> Any:
        await self.rewrite_for_critique(
            critique,
            critique_artifact_id,
            fact_issues=fact_issues,
            bundle_artifact_ids=bundle_artifact_ids,
        )
        return await self.extract_validate_claim_bundle(
            critique=critique,
            critique_artifact_id=critique_artifact_id,
            pass_key="post_repair",
        )


async def extract_validate_claim_bundle(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    evidence_toolset: Any,
    evidence_tools: list[Any],
    dossiers: list[ActDossier],
    drafts: list[ActDraft],
    critique: Any,
    critique_artifact_id: str,
    draft_artifact_ids_by_act: dict[int, str],
    source_artifact_ids: list[str],
    quote_artifact_ids: list[str],
    vector_store_id: str | None,
    run_view: Any,
    show_bible: ShowBibleContext,
    pass_key: str,
) -> Any:
    await start_phase(run, db, V2Phase.FACT_CITATION, "Extracting claims and linking citations", 80)
    bundle = await extract_and_link_claims(
        db,
        run,
        runtime,
        source_cards=source_cards,
        quote_cards=quote_cards,
        evidence_records=await list_evidence_dicts(db, run.id),
        evidence_tools=evidence_tools,
        dossiers=dossiers,
        drafts=drafts,
        critique=critique,
        vector_store_id=vector_store_id,
        hosted_file_search_handler=evidence_toolset.persist_hosted_file_search_calls,
        show_bible=show_bible,
    )
    input_artifact_ids = [
        critique_artifact_id,
        *draft_artifact_ids_by_act.values(),
        *source_artifact_ids,
        *quote_artifact_ids,
    ]
    return await validate_and_save_claim_bundle(
        db,
        run,
        runtime,
        bundle,
        source_cards=source_cards,
        quote_cards=quote_cards,
        run_view=run_view,
        evidence_tools=evidence_tools,
        input_artifact_ids=input_artifact_ids,
        prompt_hash=runtime.prompt_hash_for("v2_fact_citation", "citation_linker"),
        pass_key=pass_key,
        metadata=runtime.fallback_metadata_for("v2_fact_citation", "citation_linker"),
    )


__all__ = ["PostDraftingStageContext", "PostDraftingStageRunner", "extract_validate_claim_bundle"]
