from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import (
    audit_research_memo,
    build_act_dossier,
    build_research_memo,
    build_treatment,
    critique_episode,
    rewrite_act_draft,
    extract_and_link_claims,
    update_continuity_ledger,
    write_act_draft,
)
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.finalize import generate_audio_from_segments, transcript_payload, write_v2_script_segments
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import ClaimCitationBundle, ContinuityLedger
from app.generation_v2.source_workspace import build_source_workspace
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType
from app.pipeline.step_control import assert_step_not_stopped
from app.realtime.events import phase_completed, phase_started, progress, step_completed, step_started
from app.realtime.publisher import publish_event, update_generation_progress


async def execute_generation_v2(
    run: GenerationRun,
    db: AsyncSession,
    *,
    generate_audio: bool = True,
) -> str | None:
    runtime = V2Runtime()

    await _phase(run, db, "v2_source_workspace", "Building native source workspace", 4)
    workspace_result = await build_source_workspace(db, run)
    for card in workspace_result.source_cards:
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_CARD,
            artifact_key=card.source_id,
            payload=card,
            status=V2ArtifactStatus.PASSED,
        )
    for idx, quote in enumerate(workspace_result.quote_cards):
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.QUOTE_CARD,
            artifact_key=f"{quote.source_id}:{idx}",
            payload=quote,
            status=V2ArtifactStatus.PASSED,
        )
    await _commit_step(run, db, "source_workspace", 12)

    await _phase(run, db, "v2_editorial_research", "Building editorial research memo", 14)
    memo = await build_research_memo(
        db,
        run,
        runtime,
        source_cards=workspace_result.source_cards,
        quote_cards=workspace_result.quote_cards,
        vector_store_id=workspace_result.vector_store_id,
    )
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_MEMO,
        artifact_key="main",
        payload=memo,
        provider="openai" if workspace_result.vector_store_id else None,
        model=runtime.model,
        status=V2ArtifactStatus.PASSED,
    )
    for idx, atom in enumerate(memo.editorial_atoms):
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.EDITORIAL_ATOM,
            artifact_key=f"{idx + 1}:{atom.kind}",
            payload=atom,
            status=V2ArtifactStatus.PASSED,
        )
    await _commit_step(run, db, "editorial_research", 24)

    await _phase(run, db, "v2_research_audit", "Auditing research readiness", 26)
    audit = await audit_research_memo(
        db,
        run,
        runtime,
        research_memo=memo,
        vector_store_id=workspace_result.vector_store_id,
    )
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_AUDIT,
        artifact_key="main",
        payload=audit,
        status=V2ArtifactStatus.PASSED if audit.passed else V2ArtifactStatus.FAILED,
    )
    if not audit.passed:
        await db.commit()
        raise RuntimeError(f"V2 research audit failed: {audit.verdict}")
    await _commit_step(run, db, "research_audit", 32)

    await _phase(run, db, "v2_treatment", "Creating episode treatment", 34)
    treatment = await build_treatment(
        db,
        run,
        runtime,
        research_memo=memo,
        vector_store_id=workspace_result.vector_store_id,
    )
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.TREATMENT,
        artifact_key="main",
        payload=treatment,
        provider="openai" if workspace_result.vector_store_id else None,
        model=runtime.model,
        status=V2ArtifactStatus.PASSED,
    )
    await _commit_step(run, db, "treatment", 42)

    ledger = ContinuityLedger()
    drafts = []
    dossiers = []
    total_acts = max(1, len(treatment.acts))
    for idx, treatment_act in enumerate(treatment.acts, start=1):
        await _phase(run, db, "v2_act_drafting", f"Writing act {treatment_act.act_number}", 42 + (idx - 1) * 28 / total_acts)
        dossier = build_act_dossier(
            treatment_act=treatment_act,
            source_cards=workspace_result.source_cards,
            quote_cards=workspace_result.quote_cards,
            ledger=ledger,
        )
        dossiers.append(dossier)
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.ACT_DOSSIER,
            artifact_key=f"act-{treatment_act.act_number}",
            payload=dossier,
            status=V2ArtifactStatus.PASSED,
        )
        draft = await write_act_draft(
            db,
            run,
            runtime,
            treatment_act=treatment_act,
            dossier=dossier,
            ledger=ledger,
            vector_store_id=workspace_result.vector_store_id,
        )
        drafts.append(draft)
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.ACT_DRAFT,
            artifact_key=f"act-{draft.act_number}",
            payload=draft,
            provider="openai" if workspace_result.vector_store_id else None,
            model=runtime.model,
            status=V2ArtifactStatus.PASSED,
        )
        ledger = update_continuity_ledger(ledger, draft)
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.CONTINUITY_LEDGER,
            artifact_key=f"after-act-{draft.act_number}",
            payload=ledger,
            status=V2ArtifactStatus.PASSED,
        )
        await _commit_step(run, db, f"act_{draft.act_number}", 42 + idx * 28 / total_acts)

    await _phase(run, db, "v2_critique_rewrite", "Critiquing complete draft", 72)
    critique = await critique_episode(
        db,
        run,
        runtime,
        treatment=treatment,
        source_cards=workspace_result.source_cards,
        quote_cards=workspace_result.quote_cards,
        drafts=drafts,
        vector_store_id=workspace_result.vector_store_id,
    )
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.EPISODE_CRITIQUE,
        artifact_key="main",
        payload=critique,
        status=V2ArtifactStatus.PASSED if critique.passed else V2ArtifactStatus.FAILED,
    )
    if not critique.passed and critique.rewrite_instructions:
        revised_drafts = []
        rewrite_ledger = ContinuityLedger()
        for treatment_act, dossier, draft in zip(treatment.acts, dossiers, drafts, strict=False):
            revised = await rewrite_act_draft(
                db,
                run,
                runtime,
                treatment_act=treatment_act,
                dossier=dossier,
                draft=draft,
                ledger=rewrite_ledger,
                critique=critique,
                vector_store_id=workspace_result.vector_store_id,
            )
            revised_drafts.append(revised)
            rewrite_ledger = update_continuity_ledger(rewrite_ledger, revised)
            await save_artifact(
                db,
                generation_run_id=run.id,
                artifact_type=V2ArtifactType.ACT_DRAFT,
                artifact_key=f"act-{revised.act_number}",
                payload=revised,
                provider="openai" if workspace_result.vector_store_id else None,
                model=runtime.model,
                status=V2ArtifactStatus.PASSED,
            )
        drafts = revised_drafts
        ledger = rewrite_ledger
    await _commit_step(run, db, "critique", 78)

    await _phase(run, db, "v2_fact_citation", "Extracting claims and linking citations", 80)
    bundle = await extract_and_link_claims(
        db,
        run,
        runtime,
        source_cards=workspace_result.source_cards,
        quote_cards=workspace_result.quote_cards,
        drafts=drafts,
        critique=critique,
        vector_store_id=workspace_result.vector_store_id,
    )
    await _save_claim_bundle(db, run, bundle)

    if bundle.fact_issues:
        repaired_drafts = []
        repair_ledger = ContinuityLedger()
        for treatment_act, dossier, draft in zip(treatment.acts, dossiers, drafts, strict=False):
            act_issues = [issue for issue in bundle.fact_issues if issue.act_number == draft.act_number]
            if act_issues:
                repaired = await rewrite_act_draft(
                    db,
                    run,
                    runtime,
                    treatment_act=treatment_act,
                    dossier=dossier,
                    draft=draft,
                    ledger=repair_ledger,
                    critique=critique,
                    fact_issues=act_issues,
                    vector_store_id=workspace_result.vector_store_id,
                )
                await save_artifact(
                    db,
                    generation_run_id=run.id,
                    artifact_type=V2ArtifactType.ACT_DRAFT,
                    artifact_key=f"act-{repaired.act_number}",
                    payload=repaired,
                    provider="openai" if workspace_result.vector_store_id else None,
                    model=runtime.model,
                    status=V2ArtifactStatus.PASSED,
                )
            else:
                repaired = draft
            repaired_drafts.append(repaired)
            repair_ledger = update_continuity_ledger(repair_ledger, repaired)
        drafts = repaired_drafts
        ledger = repair_ledger
        bundle = await extract_and_link_claims(
            db,
            run,
            runtime,
            source_cards=workspace_result.source_cards,
            quote_cards=workspace_result.quote_cards,
            drafts=drafts,
            critique=critique,
            vector_store_id=workspace_result.vector_store_id,
        )
        await _save_claim_bundle(db, run, bundle)

    final = transcript_payload(treatment, drafts, bundle)
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.FINAL_TRANSCRIPT,
        artifact_key="main",
        payload=final,
        status=V2ArtifactStatus.PASSED,
    )
    segment_ids = await write_v2_script_segments(db, run, drafts=drafts)
    await _commit_step(run, db, "fact_citation", 88)

    if not generate_audio:
        return None

    await _phase(run, db, "v2_audio", "Generating audio from V2 transcript", 90)
    await db.commit()
    episode_id = await generate_audio_from_segments(run, script_segment_ids=segment_ids, quality_metrics=final.quality_metrics)
    await _commit_step(run, db, "audio", 100)
    return episode_id


async def _phase(run: GenerationRun, db: AsyncSession, phase: str, message: str, pct: float) -> None:
    await assert_step_not_stopped()
    run.current_phase = phase
    run.current_step = phase
    run.progress_pct = float(pct)
    await db.commit()
    publish_event(str(run.id), phase_started(phase, message))
    publish_event(str(run.id), step_started(phase, phase, message))
    publish_event(str(run.id), progress(float(pct), message))
    update_generation_progress(str(run.id), phase, phase, float(pct))


async def _commit_step(run: GenerationRun, db: AsyncSession, step: str, pct: float) -> None:
    await assert_step_not_stopped()
    run.current_step = step
    run.progress_pct = float(pct)
    await db.commit()
    publish_event(str(run.id), step_completed(run.current_phase or "v2", step))
    publish_event(str(run.id), progress(float(pct), step.replace("_", " ")))
    update_generation_progress(str(run.id), run.current_phase or "v2", step, float(pct))


def phase_completed_event(run_id: str, phase: str) -> None:
    publish_event(run_id, phase_completed(phase, int(time.time() * 1000)))


async def _save_claim_bundle(db: AsyncSession, run: GenerationRun, bundle: ClaimCitationBundle) -> None:
    for claim in bundle.claims:
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.DRAFT_CLAIM,
            artifact_key=claim.claim_id,
            payload=claim,
            status=V2ArtifactStatus.PASSED if claim.support_level != "unsupported" else V2ArtifactStatus.FAILED,
        )
    for idx, link in enumerate(bundle.citation_links):
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.CITATION_LINK,
            artifact_key=f"{link.claim_id}:{idx}",
            payload=link,
            status=V2ArtifactStatus.PASSED,
        )
    for issue in bundle.fact_issues:
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.FACT_ISSUE,
            artifact_key=issue.claim_id,
            payload=issue,
            status=V2ArtifactStatus.FAILED,
        )
