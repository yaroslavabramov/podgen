from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_act_dossier, update_continuity_ledger, write_act_draft
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.contracts.pipeline import V2Phase
from app.generation_v2.pipeline.status import (
    act_commit_progress,
    act_phase_progress,
    act_step_name,
    commit_step,
    next_step_after_act,
    start_phase,
)
from app.generation_v2.pipeline.validation import validate_dossier as _validate_dossier
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    ContinuityLedger,
    QuoteCard,
    ShowBibleContext,
    SourceCard,
    Treatment,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType


@dataclass
class ActDraftingResult:
    drafts: list[ActDraft]
    dossiers: list[ActDossier]
    dossier_artifact_ids_by_act: dict[int, str]
    draft_artifact_ids_by_act: dict[int, str]


async def draft_treatment_acts(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment: Treatment,
    treatment_artifact_id: str,
    triage_artifact_id: str,
    source_artifact_ids: list[str],
    quote_artifact_ids: list[str],
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    source_triage: Any,
    evidence_tools: list[Any],
    vector_store_id: str | None,
    show_bible: ShowBibleContext,
) -> ActDraftingResult:
    ledger = ContinuityLedger()
    drafts: list[ActDraft] = []
    dossiers: list[ActDossier] = []
    dossier_artifact_ids_by_act: dict[int, str] = {}
    draft_artifact_ids_by_act: dict[int, str] = {}
    ledger_artifact_id: str | None = None
    total_acts = max(1, len(treatment.acts))

    for idx, treatment_act in enumerate(treatment.acts, start=1):
        await start_phase(
            run,
            db,
            V2Phase.ACT_DRAFTING,
            f"Writing act {treatment_act.act_number}",
            act_phase_progress(idx, total_acts),
        )
        dossier = build_act_dossier(
            treatment_act=treatment_act,
            source_cards=source_cards,
            quote_cards=quote_cards,
            ledger=ledger,
            show_bible=show_bible,
            source_triage=source_triage,
        )
        dossier_gate = _validate_dossier(dossier, source_cards)
        dossiers.append(dossier)
        dossier_input_artifact_ids = [
            treatment_artifact_id,
            triage_artifact_id,
            *source_artifact_ids,
            *quote_artifact_ids,
            *([ledger_artifact_id] if ledger_artifact_id else []),
        ]
        dossier_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.ACT_DOSSIER,
            artifact_key=f"act-{treatment_act.act_number}",
            payload=dossier,
            status=V2ArtifactStatus.PASSED if dossier_gate.passed else V2ArtifactStatus.FAILED,
            input_artifact_ids=dossier_input_artifact_ids,
        )
        dossier_artifact_ids_by_act[treatment_act.act_number] = str(dossier_artifact.id)
        if not dossier_gate.passed:
            await db.commit()
            raise RuntimeError(f"V2 act dossier gate failed for act {dossier.act_number}: {dossier_gate.summary}")

        draft = await write_act_draft(
            db,
            run,
            runtime,
            treatment_act=treatment_act,
            dossier=dossier,
            ledger=ledger,
            evidence_tools=evidence_tools,
            show_bible=show_bible,
        )
        drafts.append(draft)
        draft_input_artifact_ids = [
            treatment_artifact_id,
            str(dossier_artifact.id),
            *([ledger_artifact_id] if ledger_artifact_id else []),
        ]
        draft_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.ACT_DRAFT,
            artifact_key=f"act-{draft.act_number}",
            payload=draft,
            provider="openai" if vector_store_id else None,
            model=runtime.model_for("v2_act_drafting", f"act_writer_{treatment_act.act_number}"),
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=draft_input_artifact_ids,
            prompt_hash=runtime.prompt_hash_for("v2_act_drafting", f"act_writer_{treatment_act.act_number}"),
            metadata=runtime.fallback_metadata_for("v2_act_drafting", f"act_writer_{treatment_act.act_number}"),
        )
        draft_artifact_ids_by_act[draft.act_number] = str(draft_artifact.id)

        ledger = update_continuity_ledger(ledger, draft)
        ledger_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.CONTINUITY_LEDGER,
            artifact_key=f"after-act-{draft.act_number}",
            payload=ledger,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[str(draft_artifact.id)],
        )
        ledger_artifact_id = str(ledger_artifact.id)
        await commit_step(
            run,
            db,
            act_step_name(draft.act_number),
            act_commit_progress(idx, total_acts),
            next_step=next_step_after_act(idx, total_acts),
        )

    return ActDraftingResult(
        drafts=drafts,
        dossiers=dossiers,
        dossier_artifact_ids_by_act=dossier_artifact_ids_by_act,
        draft_artifact_ids_by_act=draft_artifact_ids_by_act,
    )
