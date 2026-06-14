from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
from app.generation_v2.citation_validation import (
    merge_validation_issues,
    validate_citations,
    validation_quality_metrics,
)
from app.generation_v2.finalize import build_v2_shownotes, generate_audio_from_segments, transcript_payload, write_v2_script_segments
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ActDossier,
    CitationValidationReport,
    ClaimCitationBundle,
    ContinuityLedger,
    EpisodeCritique,
    FactIssue,
    QuoteCard,
    ShowBibleContext,
    SourceCard,
    Treatment,
)
from app.generation_v2.source_triage import build_source_triage
from app.generation_v2.source_workspace import build_source_workspace
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType
from app.models.show_bible import ShowBible
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
    show_bible = await _load_show_bible_context(db, run)
    source_artifact_ids: list[str] = []
    quote_artifact_ids: list[str] = []

    await _phase(run, db, "v2_source_workspace", "Building native source workspace", 4)
    workspace_result = await build_source_workspace(db, run)
    for card in workspace_result.source_cards:
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_CARD,
            artifact_key=card.source_id,
            payload=card,
            status=V2ArtifactStatus.PASSED,
        )
        source_artifact_ids.append(str(artifact.id))
    for idx, quote in enumerate(workspace_result.quote_cards):
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.QUOTE_CARD,
            artifact_key=f"{quote.source_id}:{idx}",
            payload=quote,
            status=V2ArtifactStatus.PASSED,
        )
        quote_artifact_ids.append(str(artifact.id))
    source_triage = build_source_triage(
        source_cards=workspace_result.source_cards,
        quote_cards=workspace_result.quote_cards,
        user_prompt=str((run.config or {}).get("user_prompt") or ""),
    )
    triage_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="main",
        payload=source_triage,
        status=V2ArtifactStatus.PASSED,
        input_artifact_ids=[*source_artifact_ids, *quote_artifact_ids],
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
        show_bible=show_bible,
    )
    source_workspace_artifact_ids = [*source_artifact_ids, *quote_artifact_ids, str(triage_artifact.id)]
    memo_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_MEMO,
        artifact_key="main",
        payload=memo,
        provider="openai" if workspace_result.vector_store_id else None,
        model=runtime.model,
        status=V2ArtifactStatus.PASSED,
        input_artifact_ids=source_workspace_artifact_ids,
        prompt_hash=runtime.prompt_hash_for("v2_editorial_research", "research_agent"),
        metadata=runtime.fallback_metadata_for("v2_editorial_research", "research_agent"),
    )
    atom_artifact_ids: list[str] = []
    for idx, atom in enumerate(memo.editorial_atoms):
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.EDITORIAL_ATOM,
            artifact_key=f"{idx + 1}:{atom.kind}",
            payload=atom,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[str(memo_artifact.id)],
            parent_artifact_ids=[str(memo_artifact.id)],
        )
        atom_artifact_ids.append(str(artifact.id))
    await _commit_step(run, db, "editorial_research", 24)

    await _phase(run, db, "v2_research_audit", "Auditing research readiness", 26)
    audit = await audit_research_memo(
        db,
        run,
        runtime,
        research_memo=memo,
        vector_store_id=workspace_result.vector_store_id,
        show_bible=show_bible,
    )
    audit_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_AUDIT,
        artifact_key="main",
        payload=audit,
        status=V2ArtifactStatus.PASSED if audit.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[str(memo_artifact.id), *atom_artifact_ids],
        prompt_hash=runtime.prompt_hash_for("v2_research_audit", "research_auditor"),
        metadata=runtime.fallback_metadata_for("v2_research_audit", "research_auditor"),
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
        show_bible=show_bible,
    )
    treatment_gate = _validate_treatment(treatment, workspace_result.source_cards, int((run.config or {}).get("duration_target", 1800)))
    treatment_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.TREATMENT,
        artifact_key="main",
        payload=treatment,
        provider="openai" if workspace_result.vector_store_id else None,
        model=runtime.model,
        status=V2ArtifactStatus.PASSED if treatment_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[str(memo_artifact.id), str(audit_artifact.id), *atom_artifact_ids],
        prompt_hash=runtime.prompt_hash_for("v2_treatment", "story_editor"),
        metadata=runtime.fallback_metadata_for("v2_treatment", "story_editor"),
    )
    if not treatment_gate.passed:
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.RESEARCH_AUDIT,
            artifact_key="treatment_gate",
            payload=treatment_gate,
            status=V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(treatment_artifact.id), *source_artifact_ids],
        )
        await db.commit()
        raise RuntimeError(f"V2 treatment gate failed: {treatment_gate.summary}")
    await _commit_step(run, db, "treatment", 42)

    ledger = ContinuityLedger()
    drafts = []
    dossiers = []
    dossier_artifact_ids_by_act: dict[int, str] = {}
    draft_artifact_ids_by_act: dict[int, str] = {}
    ledger_artifact_id: str | None = None
    total_acts = max(1, len(treatment.acts))
    for idx, treatment_act in enumerate(treatment.acts, start=1):
        await _phase(run, db, "v2_act_drafting", f"Writing act {treatment_act.act_number}", 42 + (idx - 1) * 28 / total_acts)
        dossier = build_act_dossier(
            treatment_act=treatment_act,
            source_cards=workspace_result.source_cards,
            quote_cards=workspace_result.quote_cards,
            ledger=ledger,
            show_bible=show_bible,
            source_triage=source_triage,
        )
        dossier_gate = _validate_dossier(dossier, workspace_result.source_cards)
        dossiers.append(dossier)
        dossier_input_artifact_ids = [
            str(treatment_artifact.id),
            str(triage_artifact.id),
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
            vector_store_id=workspace_result.vector_store_id,
            show_bible=show_bible,
        )
        drafts.append(draft)
        draft_input_artifact_ids = [
            str(treatment_artifact.id),
            str(dossier_artifact.id),
            *([ledger_artifact_id] if ledger_artifact_id else []),
        ]
        draft_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.ACT_DRAFT,
            artifact_key=f"act-{draft.act_number}",
            payload=draft,
            provider="openai" if workspace_result.vector_store_id else None,
            model=runtime.model,
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
        show_bible=show_bible,
    )
    critique = _apply_forbidden_phrase_gate(critique, drafts, show_bible)
    critique_input_artifact_ids = [
        str(treatment_artifact.id),
        *draft_artifact_ids_by_act.values(),
        *source_artifact_ids,
        *quote_artifact_ids,
    ]
    critique_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.EPISODE_CRITIQUE,
        artifact_key="main",
        payload=critique,
        status=V2ArtifactStatus.PASSED if critique.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=critique_input_artifact_ids,
        prompt_hash=runtime.prompt_hash_for("v2_critique_rewrite", "episode_critic"),
        metadata=runtime.fallback_metadata_for("v2_critique_rewrite", "episode_critic"),
    )
    if not critique.passed and critique.rewrite_instructions:
        revised_drafts = []
        rewrite_ledger = ContinuityLedger()
        rewritten_draft_artifact_ids_by_act: dict[int, str] = {}
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
                show_bible=show_bible,
            )
            revised_drafts.append(revised)
            rewrite_ledger = update_continuity_ledger(rewrite_ledger, revised)
            rewrite_input_artifact_ids = [
                str(critique_artifact.id),
                str(treatment_artifact.id),
                draft_artifact_ids_by_act.get(draft.act_number, ""),
                dossier_artifact_ids_by_act.get(treatment_act.act_number, ""),
            ]
            rewrite_input_artifact_ids = [artifact_id for artifact_id in rewrite_input_artifact_ids if artifact_id]
            revised_artifact = await save_artifact(
                db,
                generation_run_id=run.id,
                artifact_type=V2ArtifactType.ACT_DRAFT,
                artifact_key=f"act-{revised.act_number}",
                payload=revised,
                provider="openai" if workspace_result.vector_store_id else None,
                model=runtime.model,
                status=V2ArtifactStatus.PASSED,
                input_artifact_ids=rewrite_input_artifact_ids,
                prompt_hash=runtime.prompt_hash_for("v2_critique_rewrite", f"act_rewriter_{treatment_act.act_number}"),
                metadata=runtime.fallback_metadata_for(
                    "v2_critique_rewrite",
                    f"act_rewriter_{treatment_act.act_number}",
                ),
            )
            rewritten_draft_artifact_ids_by_act[revised.act_number] = str(revised_artifact.id)
        drafts = revised_drafts
        ledger = rewrite_ledger
        draft_artifact_ids_by_act = rewritten_draft_artifact_ids_by_act
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
        show_bible=show_bible,
    )
    claim_input_artifact_ids = [
        str(critique_artifact.id),
        *draft_artifact_ids_by_act.values(),
        *source_artifact_ids,
        *quote_artifact_ids,
    ]
    bundle, bundle_artifact_ids, validation_report, validation_artifact_id = await _validate_and_save_claim_bundle(
        db,
        run,
        bundle,
        source_cards=workspace_result.source_cards,
        quote_cards=workspace_result.quote_cards,
        input_artifact_ids=claim_input_artifact_ids,
        prompt_hash=runtime.prompt_hash_for("v2_fact_citation", "citation_linker"),
        pass_key="initial",
        metadata=runtime.fallback_metadata_for("v2_fact_citation", "citation_linker"),
    )

    if bundle.fact_issues:
        repaired_drafts = []
        repair_ledger = ContinuityLedger()
        repaired_draft_artifact_ids_by_act: dict[int, str] = {}
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
                    show_bible=show_bible,
                )
                repair_input_artifact_ids = [
                    str(critique_artifact.id),
                    str(treatment_artifact.id),
                    draft_artifact_ids_by_act.get(draft.act_number, ""),
                    dossier_artifact_ids_by_act.get(treatment_act.act_number, ""),
                    *bundle_artifact_ids,
                ]
                repair_input_artifact_ids = [artifact_id for artifact_id in repair_input_artifact_ids if artifact_id]
                repaired_artifact = await save_artifact(
                    db,
                    generation_run_id=run.id,
                    artifact_type=V2ArtifactType.ACT_DRAFT,
                    artifact_key=f"act-{repaired.act_number}",
                    payload=repaired,
                    provider="openai" if workspace_result.vector_store_id else None,
                    model=runtime.model,
                    status=V2ArtifactStatus.PASSED,
                    input_artifact_ids=repair_input_artifact_ids,
                    prompt_hash=runtime.prompt_hash_for("v2_critique_rewrite", f"act_rewriter_{treatment_act.act_number}"),
                    metadata=runtime.fallback_metadata_for(
                        "v2_critique_rewrite",
                        f"act_rewriter_{treatment_act.act_number}",
                    ),
                )
                repaired_draft_artifact_ids_by_act[repaired.act_number] = str(repaired_artifact.id)
            else:
                repaired = draft
                existing_artifact_id = draft_artifact_ids_by_act.get(draft.act_number)
                if existing_artifact_id:
                    repaired_draft_artifact_ids_by_act[draft.act_number] = existing_artifact_id
            repaired_drafts.append(repaired)
            repair_ledger = update_continuity_ledger(repair_ledger, repaired)
        drafts = repaired_drafts
        ledger = repair_ledger
        draft_artifact_ids_by_act = repaired_draft_artifact_ids_by_act
        bundle = await extract_and_link_claims(
            db,
            run,
            runtime,
            source_cards=workspace_result.source_cards,
            quote_cards=workspace_result.quote_cards,
            drafts=drafts,
            critique=critique,
            vector_store_id=workspace_result.vector_store_id,
            show_bible=show_bible,
        )
        claim_input_artifact_ids = [
            str(critique_artifact.id),
            *draft_artifact_ids_by_act.values(),
            *source_artifact_ids,
            *quote_artifact_ids,
        ]
        bundle, bundle_artifact_ids, validation_report, validation_artifact_id = await _validate_and_save_claim_bundle(
            db,
            run,
            bundle,
            source_cards=workspace_result.source_cards,
            quote_cards=workspace_result.quote_cards,
            input_artifact_ids=claim_input_artifact_ids,
            prompt_hash=runtime.prompt_hash_for("v2_fact_citation", "citation_linker"),
            pass_key="post_repair",
            metadata=runtime.fallback_metadata_for("v2_fact_citation", "citation_linker"),
        )

    blocking_issues = _blocking_fact_issues(bundle.fact_issues)
    if blocking_issues:
        await db.commit()
        raise RuntimeError(f"V2 fact gate failed with {len(blocking_issues)} blocking issue(s)")

    final = transcript_payload(treatment, drafts, bundle, source_cards=workspace_result.source_cards)
    final.quality_metrics.update(validation_quality_metrics(validation_report))
    final.quality_metrics["runtime_fallback_used"] = runtime.fallback_used
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.FINAL_TRANSCRIPT,
        artifact_key="main",
        payload=final,
        status=V2ArtifactStatus.PASSED,
        input_artifact_ids=[
            str(treatment_artifact.id),
            *draft_artifact_ids_by_act.values(),
            *bundle_artifact_ids,
            validation_artifact_id,
        ],
    )
    if runtime.fallback_used and generate_audio and not settings.generation_v2_allow_fallback_audio:
        await db.commit()
        raise RuntimeError("V2 runtime fallback was used; audio generation is disabled for fallback runs")
    segment_ids = await write_v2_script_segments(db, run, drafts=drafts)
    await _commit_step(run, db, "fact_citation", 88)

    if not generate_audio:
        return None

    await _phase(run, db, "v2_audio", "Generating audio from V2 transcript", 90)
    await db.commit()
    shownotes = build_v2_shownotes(final=final, bundle=bundle, source_cards=workspace_result.source_cards)
    episode_id = await generate_audio_from_segments(
        run,
        script_segment_ids=segment_ids,
        quality_metrics=final.quality_metrics,
        shownotes_json=shownotes,
    )
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


async def _save_claim_bundle(
    db: AsyncSession,
    run: GenerationRun,
    bundle: ClaimCitationBundle,
    *,
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    metadata: dict | None = None,
) -> list[str]:
    artifact_ids: list[str] = []
    claim_artifact_ids_by_claim: dict[str, str] = {}
    for claim in bundle.claims:
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.DRAFT_CLAIM,
            artifact_key=claim.claim_id,
            payload=claim,
            status=V2ArtifactStatus.PASSED if claim.support_level != "unsupported" else V2ArtifactStatus.FAILED,
            input_artifact_ids=input_artifact_ids,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_id = str(artifact.id)
        artifact_ids.append(artifact_id)
        claim_artifact_ids_by_claim[claim.claim_id] = artifact_id
    for idx, link in enumerate(bundle.citation_links):
        link_inputs = [
            *input_artifact_ids,
            *([claim_artifact_ids_by_claim[link.claim_id]] if link.claim_id in claim_artifact_ids_by_claim else []),
        ]
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.CITATION_LINK,
            artifact_key=f"{link.claim_id}:{idx}",
            payload=link,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=link_inputs,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_ids.append(str(artifact.id))
    for issue in bundle.fact_issues:
        issue_inputs = [
            *input_artifact_ids,
            *([claim_artifact_ids_by_claim[issue.claim_id]] if issue.claim_id in claim_artifact_ids_by_claim else []),
        ]
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.FACT_ISSUE,
            artifact_key=issue.claim_id,
            payload=issue,
            status=V2ArtifactStatus.FAILED,
            input_artifact_ids=issue_inputs,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_ids.append(str(artifact.id))
    return artifact_ids


async def _validate_and_save_claim_bundle(
    db: AsyncSession,
    run: GenerationRun,
    bundle: ClaimCitationBundle,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    pass_key: str,
    metadata: dict | None = None,
) -> tuple[ClaimCitationBundle, list[str], CitationValidationReport, str]:
    validation_report = await validate_citations(
        db,
        bundle=bundle,
        source_cards=source_cards,
        quote_cards=quote_cards,
    )
    bundle = merge_validation_issues(bundle, validation_report)
    bundle_artifact_ids = await _save_claim_bundle(
        db,
        run,
        bundle,
        input_artifact_ids=input_artifact_ids,
        prompt_hash=prompt_hash,
        metadata=metadata,
    )
    validation_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.CITATION_VALIDATION,
        artifact_key=pass_key,
        payload=validation_report,
        status=V2ArtifactStatus.PASSED if validation_report.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[*input_artifact_ids, *bundle_artifact_ids],
        prompt_hash=prompt_hash,
        metadata=metadata,
    )
    return bundle, bundle_artifact_ids, validation_report, str(validation_artifact.id)


async def _load_show_bible_context(db: AsyncSession, run: GenerationRun) -> ShowBibleContext:
    if not run.show_bible_id:
        return ShowBibleContext()
    result = await db.execute(select(ShowBible).where(ShowBible.id == run.show_bible_id))
    bible = result.scalar_one_or_none()
    if bible is None:
        return ShowBibleContext()
    return ShowBibleContext(
        show_style=bible.show_style,
        tone_and_mood=bible.tone_and_mood,
        hosts=bible.hosts or [],
        forbidden_phrases=bible.forbidden_phrases or [],
        rigor_policy=bible.rigor_policy,
        adjacency_policy=bible.adjacency_policy,
        explanation_policy=bible.explanation_policy,
        format_rules=bible.format_rules or {},
    )


def _validate_treatment(treatment: Treatment, source_cards: list[SourceCard], duration_seconds: int) -> EpisodeCritique:
    source_ids = {card.source_id for card in source_cards if card.coverage_status in {"ready", "partial"}}
    issues: list[str] = []
    if duration_seconds >= 3600 and len(treatment.acts) < 6:
        issues.append("Long-form V2 treatment requires at least 6 acts.")
    for act in treatment.acts:
        if len(set(act.source_focus) & source_ids) < max(1, min(act.source_density_target, 2)):
            issues.append(f"Act {act.act_number} does not meet source density target.")
        if not act.required_atom_refs and duration_seconds >= 3600:
            issues.append(f"Act {act.act_number} lacks required editorial atom anchors.")
    return EpisodeCritique(
        passed=not issues,
        summary="Treatment gate passed." if not issues else "Treatment gate failed.",
        weak_sections=issues,
        rewrite_instructions=issues,
    )


def _validate_dossier(dossier: ActDossier, source_cards: list[SourceCard]) -> EpisodeCritique:
    known_sources = {card.source_id for card in source_cards if card.coverage_status in {"ready", "partial"}}
    issues = []
    if len(set(dossier.must_use_sources) & known_sources) < dossier.source_density_minimum:
        issues.append("Act dossier does not meet source density minimum.")
    if not dossier.quote_cards and not dossier.anchor_refs:
        issues.append("Act dossier has no quote cards or anchor refs.")
    return EpisodeCritique(
        passed=not issues,
        summary="Act dossier gate passed." if not issues else "Act dossier gate failed.",
        weak_sections=issues,
        rewrite_instructions=issues,
    )


def _apply_forbidden_phrase_gate(
    critique: EpisodeCritique,
    drafts,
    show_bible: ShowBibleContext,
) -> EpisodeCritique:
    forbidden = [phrase.strip().lower() for phrase in show_bible.forbidden_phrases if isinstance(phrase, str) and phrase.strip()]
    if not forbidden:
        return critique
    text = "\n".join(line.text for draft in drafts for line in draft.lines).lower()
    hits = [phrase for phrase in forbidden if phrase in text]
    if not hits:
        return critique
    return EpisodeCritique(
        passed=False,
        summary=f"{critique.summary} Forbidden phrase hits: {', '.join(hits[:8])}",
        repetition_issues=[*critique.repetition_issues, *[f"Forbidden phrase: {phrase}" for phrase in hits[:8]]],
        pacing_issues=critique.pacing_issues,
        weak_sections=critique.weak_sections,
        rewrite_instructions=[*critique.rewrite_instructions, *[f"Remove forbidden phrase: {phrase}" for phrase in hits[:8]]],
    )


def _blocking_fact_issues(issues: list[FactIssue]) -> list[FactIssue]:
    return [
        issue
        for issue in issues
        if issue.severity in {"medium", "high"} or (issue.severity == "low" and issue.blocking)
    ]
