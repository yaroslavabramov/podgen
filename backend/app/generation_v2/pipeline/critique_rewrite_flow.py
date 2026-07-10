from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import rewrite_act_draft, update_continuity_ledger
from app.generation_v2.pipeline.artifact_persistence import save_rewritten_act_draft_artifact
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    ContinuityLedger,
    EpisodeCritique,
    FactIssue,
    ShowBibleContext,
    Treatment,
)
from app.models.generation_run import GenerationRun


@dataclass(frozen=True)
class DraftRevisionResult:
    drafts: list[ActDraft]
    draft_artifact_ids_by_act: dict[int, str]


async def rewrite_drafts_for_critique(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment: Treatment,
    dossiers: list[ActDossier],
    drafts: list[ActDraft],
    critique: EpisodeCritique,
    critique_artifact_id: str,
    treatment_artifact_id: str,
    draft_artifact_ids_by_act: dict[int, str],
    dossier_artifact_ids_by_act: dict[int, str],
    evidence_tools: list[Any],
    show_bible: ShowBibleContext,
    has_vector_store: bool,
    fact_issues: list[FactIssue] | None = None,
    bundle_artifact_ids: list[str] | None = None,
) -> DraftRevisionResult:
    revised_drafts: list[ActDraft] = []
    revised_artifact_ids_by_act: dict[int, str] = {}
    ledger = ContinuityLedger()

    for treatment_act, dossier, draft in zip(treatment.acts, dossiers, drafts, strict=False):
        act_issues = [issue for issue in fact_issues or [] if issue.act_number == draft.act_number]
        should_rewrite = fact_issues is None or bool(act_issues)
        if should_rewrite:
            revised = await rewrite_act_draft(
                db,
                run,
                runtime,
                treatment_act=treatment_act,
                dossier=dossier,
                draft=draft,
                ledger=ledger,
                critique=critique,
                fact_issues=act_issues or None,
                evidence_tools=evidence_tools,
                show_bible=show_bible,
            )
            artifact_id = await save_rewritten_act_draft_artifact(
                db,
                run,
                runtime,
                revised,
                treatment_act_number=treatment_act.act_number,
                critique_artifact_id=critique_artifact_id,
                treatment_artifact_id=treatment_artifact_id,
                draft_artifact_id=draft_artifact_ids_by_act.get(draft.act_number),
                dossier_artifact_id=dossier_artifact_ids_by_act.get(treatment_act.act_number),
                bundle_artifact_ids=bundle_artifact_ids,
                has_vector_store=has_vector_store,
            )
            revised_artifact_ids_by_act[revised.act_number] = artifact_id
        else:
            revised = draft
            existing_artifact_id = draft_artifact_ids_by_act.get(draft.act_number)
            if existing_artifact_id:
                revised_artifact_ids_by_act[draft.act_number] = existing_artifact_id
        revised_drafts.append(revised)
        ledger = update_continuity_ledger(ledger, revised)

    return DraftRevisionResult(drafts=revised_drafts, draft_artifact_ids_by_act=revised_artifact_ids_by_act)
