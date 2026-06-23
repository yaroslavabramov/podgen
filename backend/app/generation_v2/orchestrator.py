from __future__ import annotations

import time

from dataclasses import dataclass
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.agents.editorial import (
    audit_research_memo,
    build_act_dossier,
    build_research_memo,
    build_source_acquisition_agent,
    build_source_triage_finalizer_agent,
    build_source_triage_planner_agent,
    build_source_triage_set_agent,
    build_treatment,
    critique_episode,
    rewrite_act_draft,
    extract_and_link_claims,
    judge_claim_support,
    update_continuity_ledger,
    write_act_draft,
)
from app.generation_v2.artifacts import save_artifact, save_tool_trace
from app.generation_v2.citation_validation import (
    merge_validation_issues,
    validate_citations,
    validation_quality_metrics,
)
from app.generation_v2.finalize import build_v2_shownotes, generate_audio_from_segments, transcript_payload, write_v2_script_segments
from app.generation_v2.research_workspace import load_research_workspace_artifact
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ActDossier,
    CitationValidationReport,
    ClaimCitationBundle,
    ContinuityLedger,
    EditorialAtom,
    EpisodeCritique,
    FactIssue,
    QuoteCard,
    ResearchAudit,
    ResearchMemo,
    ShowBibleContext,
    SourceCard,
    SourceMap,
    SourceTriageCompletion,
    SupportJudgmentBundle,
    Treatment,
)
from app.generation_v2.source_acquisition import (
    SourceAcquisitionToolset,
    acquisition_enabled,
    build_corpus_gap_report,
    remaining_acquisition_rounds,
    source_acquisition_report_from_rounds,
)
from app.generation_v2.source_corpus import CorpusPreparationResult, list_evidence_dicts, prepare_v2_corpus
from app.generation_v2.source_triage_workspace import (
    build_workspace_tools,
    create_repair_sets_from_gate,
    ensure_source_triage_workspace,
    load_source_triage_workspace,
    save_source_triage_workspace,
    source_set_prompt_payload,
    start_triage_source_set,
    unprocessed_source_sets,
    unresolved_source_set_summary,
    unresolved_source_sets,
    workspace_summary_for_prompt,
)
from app.generation_v2.tools.evidence import (
    EvidenceToolset,
    build_source_map_from_write_actions,
    build_source_inventory,
    mark_cited_evidence,
    validate_source_map_gate,
)
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_CITED, EVIDENCE_STATUS_SELECTED
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import RunCorpusView, V2ArtifactStatus, V2ArtifactType
from app.models.show_bible import ShowBible
from app.pipeline.step_control import assert_step_not_stopped
from app.realtime.events import phase_completed, phase_started, progress, step_completed, step_started
from app.realtime.publisher import publish_event, update_generation_progress


@dataclass
class _TriageState:
    corpus: CorpusPreparationResult
    evidence_toolset: EvidenceToolset
    evidence_tools: list
    source_inventory: SourceMap
    triage_completion: object
    source_map: SourceMap
    triage_gate: object
    source_cards: list[SourceCard]
    quote_cards: list[QuoteCard]
    source_map_artifact_id: str
    evidence_artifact_ids: list[str]
    source_artifact_ids: list[str]
    quote_artifact_ids: list[str]
    workspace_artifact_id: str | None
    triage_artifact_id: str
    triage_gate_artifact_id: str

    @property
    def source_workspace_artifact_ids(self) -> list[str]:
        return [
            self.source_map_artifact_id,
            *self.evidence_artifact_ids,
            *self.source_artifact_ids,
            *self.quote_artifact_ids,
            *([self.workspace_artifact_id] if self.workspace_artifact_id else []),
            self.triage_artifact_id,
            self.triage_gate_artifact_id,
        ]


class V2StepExecutionStopped(RuntimeError):
    def __init__(self, executed_step: str, next_step: str | None):
        super().__init__(f"V2 stopped after {executed_step}")
        self.executed_step = executed_step
        self.next_step = next_step


async def execute_generation_v2(
    run: GenerationRun,
    db: AsyncSession,
    *,
    generate_audio: bool = True,
) -> str | None:
    runtime = V2Runtime()
    show_bible = await _load_show_bible_context(db, run)
    acquisition_rounds_used = 0

    triage_state = await _prepare_and_triage(run, db, runtime)
    if not triage_state.triage_completion.ready or not triage_state.triage_gate.passed:
        triage_state, acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
            run,
            db,
            runtime,
            triage_state=triage_state,
            trigger_phase="source_triage",
            acquisition_rounds_used=acquisition_rounds_used,
        )
    if not triage_state.triage_completion.ready or not triage_state.triage_gate.passed:
        await db.commit()
        raise RuntimeError(
            f"V2 source triage gate failed: {triage_state.triage_completion.summary} {triage_state.triage_gate.summary}"
        )
    corpus = triage_state.corpus
    evidence_toolset = triage_state.evidence_toolset
    evidence_tools = triage_state.evidence_tools
    source_map = triage_state.source_map
    source_cards = triage_state.source_cards
    quote_cards = triage_state.quote_cards
    source_triage = source_map.source_triage
    source_artifact_ids = triage_state.source_artifact_ids
    quote_artifact_ids = triage_state.quote_artifact_ids
    source_map_artifact_id = triage_state.source_map_artifact_id
    source_workspace_artifact_ids = triage_state.source_workspace_artifact_ids
    run_source_cards = _merge_source_cards(triage_state.source_inventory.source_cards, source_cards)
    await _commit_step(run, db, "corpus_prepare", 12, next_step="editorial_research")

    while True:
        await _phase(run, db, "v2_editorial_research", "Building editorial research memo", 14)
        memo = await build_research_memo(
            db,
            run,
            runtime,
            source_cards=source_cards,
            quote_cards=quote_cards,
            source_map=source_map,
            source_inventory=triage_state.source_inventory,
            evidence_tools=evidence_tools,
            evidence_toolset=evidence_toolset,
            vector_store_id=corpus.vector_store_id,
            hosted_file_search_handler=evidence_toolset.persist_hosted_file_search_calls,
            show_bible=show_bible,
        )
        research_workspace_artifact = await load_research_workspace_artifact(db, run.id)
        research_workspace_artifact_ids = [str(research_workspace_artifact.id)] if research_workspace_artifact else []
        memo_evidence_artifact_ids_by_evidence_id = await _save_evidence_artifact_map(
            db,
            run,
            evidence_ids=_research_memo_evidence_ids(memo),
            input_artifact_ids=[*source_workspace_artifact_ids, *research_workspace_artifact_ids],
        )
        memo_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.RESEARCH_MEMO,
            artifact_key="main",
            payload=memo,
            provider="openai" if corpus.vector_store_id else None,
            model=runtime.model_for("v2_editorial_research", "research_finalizer_agent"),
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[
                *source_workspace_artifact_ids,
                *research_workspace_artifact_ids,
                *memo_evidence_artifact_ids_by_evidence_id.values(),
            ],
            prompt_hash=runtime.prompt_hash_for("v2_editorial_research", "research_finalizer_agent"),
            metadata=runtime.fallback_metadata_for("v2_editorial_research", "research_finalizer_agent"),
        )
        atom_artifact_ids: list[str] = []
        for idx, atom in enumerate(memo.editorial_atoms):
            atom_evidence_artifact_ids = [
                memo_evidence_artifact_ids_by_evidence_id[evidence_id]
                for evidence_id in atom.evidence_ids
                if evidence_id in memo_evidence_artifact_ids_by_evidence_id
            ]
            artifact = await save_artifact(
                db,
                generation_run_id=run.id,
                artifact_type=V2ArtifactType.EDITORIAL_ATOM,
                artifact_key=f"{idx + 1}:{atom.kind}",
                payload=atom,
                status=V2ArtifactStatus.PASSED,
                input_artifact_ids=[str(memo_artifact.id), *atom_evidence_artifact_ids],
                parent_artifact_ids=[str(memo_artifact.id)],
            )
            atom_artifact_ids.append(str(artifact.id))
        memo_evidence_gate = _validate_research_memo_evidence(
            memo,
            evidence_records=await list_evidence_dicts(db, run.id),
            run_view=corpus.run_view,
            duration_seconds=int((run.config or {}).get("duration_target", 1800)),
            source_map=source_map,
        )
        memo_gate_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.RESEARCH_AUDIT,
            artifact_key="memo_evidence_gate",
            payload=memo_evidence_gate,
            status=V2ArtifactStatus.PASSED if memo_evidence_gate.passed else V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(memo_artifact.id), *atom_artifact_ids, source_map_artifact_id],
        )
        if not memo_evidence_gate.passed:
            previous_round = acquisition_rounds_used
            triage_state, acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
                run,
                db,
                runtime,
                triage_state=triage_state,
                trigger_phase="memo_evidence",
                acquisition_rounds_used=acquisition_rounds_used,
                research_audit=memo_evidence_gate,
            )
            if acquisition_rounds_used > previous_round and triage_state.triage_completion.ready and triage_state.triage_gate.passed:
                corpus, evidence_toolset, evidence_tools, source_map, source_cards, quote_cards, source_triage = _triage_runtime_values(triage_state)
                source_artifact_ids = triage_state.source_artifact_ids
                quote_artifact_ids = triage_state.quote_artifact_ids
                source_map_artifact_id = triage_state.source_map_artifact_id
                source_workspace_artifact_ids = triage_state.source_workspace_artifact_ids
                run_source_cards = _merge_source_cards(triage_state.source_inventory.source_cards, source_cards)
                continue
            await db.commit()
            raise RuntimeError(f"V2 research memo evidence gate failed: {memo_evidence_gate.verdict}")
        await _commit_step(run, db, "editorial_research", 24, next_step="research_audit")

        await _phase(run, db, "v2_research_audit", "Auditing research readiness", 26)
        audit = await audit_research_memo(
            db,
            run,
            runtime,
            research_memo=memo,
            source_map=source_map,
            evidence_tools=evidence_tools,
            show_bible=show_bible,
        )
        audit_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.RESEARCH_AUDIT,
            artifact_key="main",
            payload=audit,
            provider="openai" if corpus.vector_store_id else None,
            model=runtime.model_for("v2_research_audit", "research_auditor"),
            status=V2ArtifactStatus.PASSED if audit.passed else V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(memo_artifact.id), *atom_artifact_ids, str(memo_gate_artifact.id)],
            prompt_hash=runtime.prompt_hash_for("v2_research_audit", "research_auditor"),
            metadata=runtime.fallback_metadata_for("v2_research_audit", "research_auditor"),
        )
        if not audit.passed:
            previous_round = acquisition_rounds_used
            triage_state, acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
                run,
                db,
                runtime,
                triage_state=triage_state,
                trigger_phase="research_audit",
                acquisition_rounds_used=acquisition_rounds_used,
                research_audit=audit,
            )
            if acquisition_rounds_used > previous_round and triage_state.triage_completion.ready and triage_state.triage_gate.passed:
                corpus, evidence_toolset, evidence_tools, source_map, source_cards, quote_cards, source_triage = _triage_runtime_values(triage_state)
                source_artifact_ids = triage_state.source_artifact_ids
                quote_artifact_ids = triage_state.quote_artifact_ids
                source_map_artifact_id = triage_state.source_map_artifact_id
                source_workspace_artifact_ids = triage_state.source_workspace_artifact_ids
                run_source_cards = _merge_source_cards(triage_state.source_inventory.source_cards, source_cards)
                continue
            await db.commit()
            raise RuntimeError(f"V2 research audit failed: {audit.verdict}")
        await _commit_step(run, db, "research_audit", 32, next_step="treatment")

        await _phase(run, db, "v2_treatment", "Creating episode treatment", 34)
        treatment = await build_treatment(
            db,
            run,
            runtime,
            research_memo=memo,
            source_map=source_map,
            source_inventory=triage_state.source_inventory,
            evidence_tools=evidence_tools,
            show_bible=show_bible,
        )
        treatment_gate = _validate_treatment(treatment, run_source_cards, int((run.config or {}).get("duration_target", 1800)))
        treatment_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.TREATMENT,
            artifact_key="main",
            payload=treatment,
            provider="openai" if corpus.vector_store_id else None,
            model=runtime.model_for("v2_treatment", "story_editor"),
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
                input_artifact_ids=[str(treatment_artifact.id), source_map_artifact_id, *source_artifact_ids],
            )
            previous_round = acquisition_rounds_used
            triage_state, acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
                run,
                db,
                runtime,
                triage_state=triage_state,
                trigger_phase="treatment",
                acquisition_rounds_used=acquisition_rounds_used,
                treatment_gate=treatment_gate,
            )
            if acquisition_rounds_used > previous_round and triage_state.triage_completion.ready and triage_state.triage_gate.passed:
                corpus, evidence_toolset, evidence_tools, source_map, source_cards, quote_cards, source_triage = _triage_runtime_values(triage_state)
                source_artifact_ids = triage_state.source_artifact_ids
                quote_artifact_ids = triage_state.quote_artifact_ids
                source_map_artifact_id = triage_state.source_map_artifact_id
                source_workspace_artifact_ids = triage_state.source_workspace_artifact_ids
                run_source_cards = _merge_source_cards(triage_state.source_inventory.source_cards, source_cards)
                continue
            await db.commit()
            raise RuntimeError(f"V2 treatment gate failed: {treatment_gate.summary}")
        await _commit_step(run, db, "treatment", 42, next_step="act_1")
        break

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
            source_cards=run_source_cards,
            quote_cards=quote_cards,
            ledger=ledger,
            show_bible=show_bible,
            source_triage=source_triage,
        )
        dossier_gate = _validate_dossier(dossier, run_source_cards)
        dossiers.append(dossier)
        dossier_input_artifact_ids = [
            str(treatment_artifact.id),
            triage_state.triage_artifact_id,
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
            provider="openai" if corpus.vector_store_id else None,
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
        next_step = f"act_{idx + 1}" if idx < total_acts else "critique"
        await _commit_step(run, db, f"act_{draft.act_number}", 42 + idx * 28 / total_acts, next_step=next_step)

    await _phase(run, db, "v2_critique_rewrite", "Critiquing complete draft", 72)
    critique = await critique_episode(
        db,
        run,
        runtime,
        treatment=treatment,
        source_cards=run_source_cards,
        quote_cards=quote_cards,
        drafts=drafts,
        evidence_tools=evidence_tools,
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
        provider="openai" if corpus.vector_store_id else None,
        model=runtime.model_for("v2_critique_rewrite", "episode_critic"),
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
                evidence_tools=evidence_tools,
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
                provider="openai" if corpus.vector_store_id else None,
                model=runtime.model_for("v2_critique_rewrite", f"act_rewriter_{treatment_act.act_number}"),
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
    await _commit_step(run, db, "critique", 78, next_step="fact_citation")

    await _phase(run, db, "v2_fact_citation", "Extracting claims and linking citations", 80)
    bundle = await extract_and_link_claims(
        db,
        run,
        runtime,
        source_cards=run_source_cards,
        quote_cards=quote_cards,
        evidence_records=await list_evidence_dicts(db, run.id),
        evidence_tools=evidence_tools,
        dossiers=dossiers,
        drafts=drafts,
        critique=critique,
        vector_store_id=corpus.vector_store_id,
        hosted_file_search_handler=evidence_toolset.persist_hosted_file_search_calls,
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
        runtime,
        bundle,
        source_cards=run_source_cards,
        quote_cards=quote_cards,
        run_view=corpus.run_view,
        evidence_tools=evidence_tools,
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
                    evidence_tools=evidence_tools,
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
                    provider="openai" if corpus.vector_store_id else None,
                    model=runtime.model_for("v2_critique_rewrite", f"act_rewriter_{treatment_act.act_number}"),
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
            source_cards=run_source_cards,
            quote_cards=quote_cards,
            evidence_records=await list_evidence_dicts(db, run.id),
            evidence_tools=evidence_tools,
            dossiers=dossiers,
            drafts=drafts,
            critique=critique,
            vector_store_id=corpus.vector_store_id,
            hosted_file_search_handler=evidence_toolset.persist_hosted_file_search_calls,
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
            runtime,
            bundle,
            source_cards=run_source_cards,
            quote_cards=quote_cards,
            run_view=corpus.run_view,
            evidence_tools=evidence_tools,
            input_artifact_ids=claim_input_artifact_ids,
            prompt_hash=runtime.prompt_hash_for("v2_fact_citation", "citation_linker"),
            pass_key="post_repair",
            metadata=runtime.fallback_metadata_for("v2_fact_citation", "citation_linker"),
        )

    blocking_issues = _blocking_fact_issues(bundle.fact_issues)
    if blocking_issues:
        await db.commit()
        raise RuntimeError(f"V2 fact gate failed with {len(blocking_issues)} blocking issue(s)")
    await mark_cited_evidence(
        db,
        generation_run_id=run.id,
        evidence_ids=[link.evidence_id for link in bundle.citation_links if link.evidence_id],
    )

    final = transcript_payload(treatment, drafts, bundle, source_cards=run_source_cards)
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
    await _commit_step(run, db, "fact_citation", 88, next_step="audio" if generate_audio else None)

    if not generate_audio:
        return None

    await _phase(run, db, "v2_audio", "Generating audio from V2 transcript", 90)
    await db.commit()
    shownotes = build_v2_shownotes(
        final=final,
        bundle=bundle,
        source_cards=run_source_cards,
        evidence_records=await list_evidence_dicts(db, run.id),
    )
    episode_id = await generate_audio_from_segments(
        run,
        script_segment_ids=segment_ids,
        quality_metrics=final.quality_metrics,
        shownotes_json=shownotes,
    )
    await _commit_step(run, db, "audio", 100, next_step=None)
    return episode_id


async def _prepare_and_triage(run: GenerationRun, db: AsyncSession, runtime: V2Runtime) -> _TriageState:
    await _phase(run, db, "v2_corpus_prepare", "Preparing evidence corpus", 4)
    corpus = await prepare_v2_corpus(db, run)
    evidence_toolset = EvidenceToolset(
        db=db,
        run=run,
        run_view=corpus.run_view,
        provider_index=corpus.provider_index,
    )
    evidence_tools = evidence_toolset.agent_tools()
    source_inventory = await build_source_inventory(db, corpus.run_view)
    workspace = await ensure_source_triage_workspace(
        db,
        run=run,
        run_view=corpus.run_view,
        inventory=source_inventory,
    )
    planner_tools = build_workspace_tools(
        db=db,
        run=run,
        workspace=workspace,
        inventory=source_inventory,
        evidence_toolset=evidence_toolset,
        mode="planner",
    )
    planner_completion = await build_source_triage_planner_agent(
        db,
        run,
        runtime,
        workspace_summary=workspace_summary_for_prompt(workspace),
        workspace_tools=planner_tools,
    )
    workspace = await load_source_triage_workspace(db, run.id) or workspace
    set_completions: list[SourceTriageCompletion] = []
    if workspace.get("status") == "planned":
        for source_set in unprocessed_source_sets(workspace):
            await assert_step_not_stopped()
            completion = await _run_source_triage_set(
                run=run,
                db=db,
                runtime=runtime,
                corpus=corpus,
                source_inventory=source_inventory,
                workspace=workspace,
                source_set=source_set,
            )
            set_completions.append(completion)
            workspace = await load_source_triage_workspace(db, run.id) or workspace
    else:
        await save_tool_trace(
            db,
            generation_run_id=run.id,
            phase="v2_source_triage",
            agent_name="source_triage_planner_agent",
            tool_name="triage_workspace_plan_not_finalized",
            input_json={"workspace_status": workspace.get("status")},
            output_json={
                "accepted": False,
                "planner_ready": planner_completion.ready,
                "summary": planner_completion.summary,
            },
        )
    workspace = await _materialize_workspace_deferrals_as_gap_traces(db=db, run=run, workspace=workspace)
    source_map = await build_source_map_from_write_actions(
        db,
        generation_run_id=run.id,
        inventory=source_inventory,
    )
    finalizer_tools = [
        *evidence_toolset.agent_tools(),
        *build_workspace_tools(
            db=db,
            run=run,
            workspace=workspace,
            inventory=source_inventory,
            evidence_toolset=evidence_toolset,
            mode="finalizer",
        ),
    ]
    unresolved_sets = unresolved_source_sets(workspace)
    if unresolved_sets:
        finalizer_completion = await _source_triage_unresolved_completion(db=db, run=run, workspace=workspace)
    else:
        finalizer_completion = await build_source_triage_finalizer_agent(
            db,
            run,
            runtime,
            workspace_summary=workspace_summary_for_prompt(workspace),
            source_map=source_map,
            finalizer_tools=finalizer_tools,
        )
    source_map = await build_source_map_from_write_actions(
        db,
        generation_run_id=run.id,
        inventory=source_inventory,
    )
    triage_completion = _combined_triage_completion(planner_completion, set_completions, finalizer_completion)
    triage_completion_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="completion",
        payload=triage_completion,
        provider="openai" if corpus.vector_store_id else None,
        model=runtime.model_for("v2_source_triage", "source_triage_finalizer_agent"),
        status=V2ArtifactStatus.PASSED if triage_completion.ready else V2ArtifactStatus.FAILED,
        prompt_hash=runtime.prompt_hash_for("v2_source_triage", "source_triage_finalizer_agent"),
        metadata=runtime.fallback_metadata_for("v2_source_triage", "source_triage_finalizer_agent"),
    )
    triage_gate = await validate_source_map_gate(
        db,
        generation_run_id=run.id,
        source_map=source_map,
        run_view=corpus.run_view,
        triage_completion=triage_completion,
    )
    if not triage_gate.passed:
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_TRIAGE,
            artifact_key="gate_pre_repair",
            payload=triage_gate,
            status=V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(triage_completion_artifact.id)],
        )
        await save_tool_trace(
            db,
            generation_run_id=run.id,
            phase="v2_source_triage",
            agent_name="source_triage_gate",
            tool_name="triage_targeted_repair_started",
            input_json={
                "gate": triage_gate.model_dump(mode="json"),
                "source_card_count": len(source_map.source_cards),
                "quote_card_count": len(source_map.quote_cards),
                "cluster_count": len(source_map.source_triage.source_clusters),
                "relationship_count": len(source_map.source_triage.source_relationships),
            },
            output_json={"targeted_repair": True},
        )
        workspace = await load_source_triage_workspace(db, run.id) or workspace
        workspace = create_repair_sets_from_gate(workspace, gate=triage_gate, inventory=source_inventory, source_map=source_map)
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
        repair_completions: list[SourceTriageCompletion] = []
        for repair_set in [item for item in unprocessed_source_sets(workspace) if item.get("repair")][:4]:
            await assert_step_not_stopped()
            repair_completion = await _run_source_triage_set(
                run=run,
                db=db,
                runtime=runtime,
                corpus=corpus,
                source_inventory=source_inventory,
                workspace=workspace,
                source_set=repair_set,
            )
            repair_completions.append(repair_completion)
            workspace = await load_source_triage_workspace(db, run.id) or workspace
        workspace = await _materialize_workspace_deferrals_as_gap_traces(db=db, run=run, workspace=workspace)
        source_map = await build_source_map_from_write_actions(
            db,
            generation_run_id=run.id,
            inventory=source_inventory,
        )
        unresolved_sets = unresolved_source_sets(workspace)
        if unresolved_sets:
            finalizer_completion = await _source_triage_unresolved_completion(db=db, run=run, workspace=workspace)
        else:
            finalizer_completion = await build_source_triage_finalizer_agent(
                db,
                run,
                runtime,
                workspace_summary=workspace_summary_for_prompt(workspace),
                source_map=source_map,
                finalizer_tools=finalizer_tools,
            )
        source_map = await build_source_map_from_write_actions(
            db,
            generation_run_id=run.id,
            inventory=source_inventory,
        )
        triage_gate = await validate_source_map_gate(
            db,
            generation_run_id=run.id,
            source_map=source_map,
            run_view=corpus.run_view,
            triage_completion=finalizer_completion,
        )
        triage_completion = SourceTriageCompletion(
            ready=finalizer_completion.ready,
            summary=f"{triage_completion.summary}\nTargeted repair pass: {finalizer_completion.summary}",
            notes=[
                *triage_completion.notes,
                "Targeted source triage repair sets executed after gate failure.",
                *[completion.summary for completion in repair_completions if completion.summary],
                *finalizer_completion.notes,
            ],
        )
    workspace = await load_source_triage_workspace(db, run.id) or workspace
    workspace_artifact = await save_source_triage_workspace(
        db,
        generation_run_id=run.id,
        workspace={**workspace, "status": "finalized" if triage_gate.passed else "failed_gate"},
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
    )
    source_cards = source_map.source_cards
    quote_cards = source_map.quote_cards
    source_map_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_MAP,
        artifact_key="main",
        payload=source_map,
        provider="openai" if corpus.vector_store_id else None,
        model=runtime.model_for("v2_source_triage", "source_triage_finalizer_agent"),
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        prompt_hash=runtime.prompt_hash_for("v2_source_triage", "source_triage_finalizer_agent"),
        metadata=runtime.fallback_metadata_for("v2_source_triage", "source_triage_finalizer_agent"),
        input_artifact_ids=[str(triage_completion_artifact.id)],
    )
    evidence_artifact_ids = await _save_evidence_artifacts(
        db,
        run,
        evidence_ids=source_map.evidence_ids,
        input_artifact_ids=[str(source_map_artifact.id)],
    )
    source_artifact_ids: list[str] = []
    quote_artifact_ids: list[str] = []
    for card in source_cards:
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_CARD,
            artifact_key=card.source_id,
            payload=card,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[str(source_map_artifact.id), *evidence_artifact_ids],
        )
        source_artifact_ids.append(str(artifact.id))
    for idx, quote in enumerate(quote_cards):
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.QUOTE_CARD,
            artifact_key=f"{quote.source_id}:{idx}",
            payload=quote,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[str(source_map_artifact.id), *evidence_artifact_ids],
        )
        quote_artifact_ids.append(str(artifact.id))
    source_triage = source_map.source_triage
    triage_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="main",
        payload=source_triage,
        provider="openai" if corpus.vector_store_id else None,
        model=runtime.model_for("v2_source_triage", "source_triage_finalizer_agent"),
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[str(triage_completion_artifact.id), str(source_map_artifact.id), *source_artifact_ids, *quote_artifact_ids],
        prompt_hash=runtime.prompt_hash_for("v2_source_triage", "source_triage_finalizer_agent"),
        metadata=runtime.fallback_metadata_for("v2_source_triage", "source_triage_finalizer_agent"),
    )
    triage_gate_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="gate",
        payload=triage_gate,
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[str(triage_completion_artifact.id), str(source_map_artifact.id), str(triage_artifact.id)],
    )
    return _TriageState(
        corpus=corpus,
        evidence_toolset=evidence_toolset,
        evidence_tools=evidence_tools,
        source_inventory=source_inventory,
        triage_completion=triage_completion,
        source_map=source_map,
        triage_gate=triage_gate,
        source_cards=source_cards,
        quote_cards=quote_cards,
        source_map_artifact_id=str(source_map_artifact.id),
        evidence_artifact_ids=evidence_artifact_ids,
        source_artifact_ids=source_artifact_ids,
        quote_artifact_ids=quote_artifact_ids,
        workspace_artifact_id=str(workspace_artifact.id),
        triage_artifact_id=str(triage_artifact.id),
        triage_gate_artifact_id=str(triage_gate_artifact.id),
    )


async def _source_triage_unresolved_completion(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
) -> SourceTriageCompletion:
    unresolved = unresolved_source_set_summary(workspace)
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase="v2_source_triage",
        agent_name="source_triage_workflow",
        tool_name="source_sets_unresolved",
        input_json={"unresolved_source_sets": unresolved},
        output_json={"accepted": False, "unresolved_set_count": len(unresolved_source_sets(workspace))},
    )
    return SourceTriageCompletion(
        ready=False,
        summary="Source triage workflow has unresolved source sets; finalization is blocked.",
        notes=[f"{item['set_id']}:{item['status']}:{','.join(item.get('source_refs') or [])}" for item in unresolved],
    )


async def _materialize_workspace_deferrals_as_gap_traces(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    materialized = {str(item) for item in workspace.get("materialized_deferred_source_refs") or []}
    deferred = [
        dict(item)
        for item in workspace.get("deferred_sources") or []
        if item.get("source_ref") and str(item.get("source_ref")) not in materialized
    ]
    if not deferred:
        return workspace
    for item in deferred:
        source_ref = str(item.get("source_ref"))
        reason = str(item.get("reason") or "Deferred by source triage workflow.")
        await save_tool_trace(
            db,
            generation_run_id=run.id,
            phase="v2_source_triage",
            agent_name="source_triage_workspace",
            tool_name="record_gap",
            input_json={
                "kind": "coverage",
                "description": f"Source {source_ref} deferred during source triage: {reason}",
                "source_refs": [source_ref],
                "evidence_ids": [],
            },
            output_json={"accepted": True, "materialized_workspace_deferral": True},
        )
        materialized.add(source_ref)
    workspace["materialized_deferred_source_refs"] = sorted(materialized)
    return await _save_and_reload_workspace(db=db, run=run, workspace=workspace)


async def _save_and_reload_workspace(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    return await load_source_triage_workspace(db, run.id) or workspace


async def _run_source_triage_set(
    *,
    run: GenerationRun,
    db: AsyncSession,
    runtime: V2Runtime,
    corpus: CorpusPreparationResult,
    source_inventory: SourceMap,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> SourceTriageCompletion:
    set_id = str(source_set.get("set_id") or "")
    if not set_id:
        return SourceTriageCompletion(ready=False, summary="Source triage set is missing set_id.", notes=[])
    workspace = await start_triage_source_set(
        db,
        run=run,
        workspace=workspace,
        set_id=set_id,
        inventory=source_inventory,
        evidence_toolset=EvidenceToolset(
            db=db,
            run=run,
            run_view=corpus.run_view,
            provider_index=corpus.provider_index,
        ),
    )
    current_set = next((item for item in workspace.get("source_sets") or [] if item.get("set_id") == set_id), source_set)
    allowed_refs = {str(ref) for ref in current_set.get("source_refs") or [] if str(ref).strip()}
    scoped_toolset = EvidenceToolset(
        db=db,
        run=run,
        run_view=corpus.run_view,
        provider_index=corpus.provider_index,
        triage_allowed_source_refs=allowed_refs,
        triage_current_set_id=set_id,
    )
    set_tools = [
        *scoped_toolset.agent_tools(),
        *build_workspace_tools(
            db=db,
            run=run,
            workspace=workspace,
            inventory=source_inventory,
            evidence_toolset=scoped_toolset,
            mode="set",
            current_set_id=set_id,
        ),
    ]
    completion = await build_source_triage_set_agent(
        db,
        run,
        runtime,
        source_set_payload=source_set_prompt_payload(workspace, set_id),
        set_tools=set_tools,
        vector_store_id=corpus.vector_store_id,
        hosted_file_search_handler=scoped_toolset.persist_hosted_file_search_calls,
    )
    latest = await load_source_triage_workspace(db, run.id) or workspace
    finished_set = next((item for item in latest.get("source_sets") or [] if item.get("set_id") == set_id), None)
    if finished_set is None or finished_set.get("status") == "in_progress":
        await _mark_source_triage_set_unfinished(
            db=db,
            run=run,
            workspace=latest,
            set_id=set_id,
            reason="Set agent returned before calling finish_triage_source_set.",
        )
        return SourceTriageCompletion(
            ready=False,
            summary=f"Source triage set {set_id} was not finished by the agent.",
            notes=[completion.summary] if completion.summary else [],
        )
    return completion


async def _mark_source_triage_set_unfinished(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    set_id: str,
    reason: str,
) -> None:
    updated_sets: list[dict[str, Any]] = []
    for item in workspace.get("source_sets") or []:
        source_set = dict(item)
        if source_set.get("set_id") == set_id:
            source_set["status"] = "needs_repair"
            source_set["repair"] = True
            source_set["deferred_reason"] = reason
            source_set["findings_summary"] = source_set.get("findings_summary") or reason
        updated_sets.append(source_set)
    workspace["source_sets"] = updated_sets
    workspace["current_set"] = None
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase="v2_source_triage",
        agent_name="source_triage_set_workflow",
        tool_name="source_set_unfinished",
        input_json={"set_id": set_id},
        output_json={"accepted": False, "error": reason},
    )


def _combined_triage_completion(
    planner_completion: SourceTriageCompletion,
    set_completions: list[SourceTriageCompletion],
    finalizer_completion: SourceTriageCompletion,
) -> SourceTriageCompletion:
    notes: list[str] = []
    if planner_completion.summary:
        notes.append(f"Planner: {planner_completion.summary}")
    for idx, completion in enumerate(set_completions, start=1):
        if completion.summary:
            notes.append(f"Set {idx}: {completion.summary}")
    notes.extend(finalizer_completion.notes)
    return SourceTriageCompletion(
        ready=finalizer_completion.ready,
        summary=finalizer_completion.summary,
        notes=notes[:60],
    )


def _source_triage_repair_context(source_map: SourceMap, triage_gate) -> dict:
    gate_payload = triage_gate.model_dump(mode="json") if hasattr(triage_gate, "model_dump") else {}
    repair_source_ids = {
        *gate_payload.get("missing_source_card_evidence_source_ids", []),
        *gate_payload.get("missing_model_read_source_ids", []),
        *gate_payload.get("missing_source_card_write_source_ids", []),
        *gate_payload.get("invalid_source_ids", []),
    }
    if not repair_source_ids:
        repair_source_ids = {card.source_id for card in source_map.source_cards[:12]}
    repair_cards = [
        card.model_dump(mode="json")
        for card in source_map.source_cards
        if card.source_id in repair_source_ids
    ]
    repair_quotes = [
        quote.model_dump(mode="json")
        for quote in source_map.quote_cards
        if quote.source_id in repair_source_ids
    ][:12]
    return {
        "gate": gate_payload,
        "source_map": {
            "source_cards_needing_repair": repair_cards,
            "quote_cards_touching_repair_sources": repair_quotes,
            "gaps": source_map.gaps,
            "contradictions": source_map.contradictions,
            "retrieval_notes": source_map.retrieval_notes,
            "top_evidence_by_function": source_map.source_triage.top_evidence_by_function,
            "source_relationships": [
                relation.model_dump(mode="json")
                for relation in source_map.source_triage.source_relationships
                if relation.source_id in repair_source_ids or relation.related_source_id in repair_source_ids
            ],
        },
    }


async def _run_source_acquisition_until_triage_ready(
    run: GenerationRun,
    db: AsyncSession,
    runtime: V2Runtime,
    *,
    triage_state: _TriageState,
    trigger_phase: str,
    acquisition_rounds_used: int,
    research_audit: ResearchAudit | None = None,
    treatment_gate: EpisodeCritique | None = None,
) -> tuple[_TriageState, int]:
    if not acquisition_enabled(run):
        return triage_state, acquisition_rounds_used
    rounds = []
    while remaining_acquisition_rounds(acquisition_rounds_used) > 0:
        round_number = acquisition_rounds_used + 1
        gap_report = build_corpus_gap_report(
            run,
            trigger_phase=trigger_phase,
            round_number=round_number,
            source_map=triage_state.source_map,
            triage_completion_summary=triage_state.triage_completion.summary,
            triage_ready=triage_state.triage_completion.ready,
            triage_gate=triage_state.triage_gate,
            research_audit=research_audit,
            treatment_gate=treatment_gate,
        )
        gap_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.CORPUS_GAP_REPORT,
            artifact_key=f"{trigger_phase}:round-{round_number}",
            payload=gap_report,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=triage_state.source_workspace_artifact_ids,
        )
        await _phase(run, db, "v2_source_acquisition", f"Acquiring sources for {trigger_phase} gaps", 10)
        toolset = SourceAcquisitionToolset(
            db=db,
            run=run,
            gap_report=gap_report,
            round_number=round_number,
        )
        completion = await build_source_acquisition_agent(
            db,
            run,
            runtime,
            gap_report=gap_report,
            source_inventory=triage_state.source_inventory,
            acquisition_tools=toolset.agent_tools(),
            round_number=round_number,
        )
        round_payload = toolset.round_artifact_payload(
            trigger_phase=trigger_phase,
            gap_report_id=str(gap_artifact.id),
        )
        if completion.summary:
            round_payload.summary = completion.summary
        acquisition_agent_name = f"source_acquisition_round_{round_number}"
        acquisition_model = runtime.model_for("v2_source_acquisition", acquisition_agent_name)
        round_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_ACQUISITION_ROUND,
            artifact_key=f"{trigger_phase}:round-{round_number}",
            payload=round_payload,
            provider="openai",
            model=acquisition_model,
            status=V2ArtifactStatus.PASSED if round_payload.accepted_count else V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(gap_artifact.id)],
            prompt_hash=runtime.prompt_hash_for("v2_source_acquisition", acquisition_agent_name),
            metadata=runtime.fallback_metadata_for("v2_source_acquisition", acquisition_agent_name),
        )
        rounds.append(round_payload)
        acquisition_rounds_used = round_number
        report = source_acquisition_report_from_rounds(
            enabled=True,
            exhausted=remaining_acquisition_rounds(acquisition_rounds_used) == 0,
            rounds=rounds,
        )
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_ACQUISITION_REPORT,
            artifact_key=trigger_phase,
            payload=report,
            provider="openai",
            model=acquisition_model,
            status=V2ArtifactStatus.PASSED if report.total_accepted else V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(gap_artifact.id), str(round_artifact.id)],
        )
        await db.commit()
        if round_payload.accepted_count <= 0:
            break
        triage_state = await _prepare_and_triage(run, db, runtime)
        if triage_state.triage_completion.ready and triage_state.triage_gate.passed:
            break
    return triage_state, acquisition_rounds_used


def _triage_runtime_values(
    triage_state: _TriageState,
) -> tuple[CorpusPreparationResult, EvidenceToolset, list, SourceMap, list[SourceCard], list[QuoteCard], object]:
    return (
        triage_state.corpus,
        triage_state.evidence_toolset,
        triage_state.evidence_tools,
        triage_state.source_map,
        triage_state.source_cards,
        triage_state.quote_cards,
        triage_state.source_map.source_triage,
    )


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


async def _commit_step(run: GenerationRun, db: AsyncSession, step: str, pct: float, *, next_step: str | None) -> None:
    await assert_step_not_stopped()
    run.current_step = step
    run.progress_pct = float(pct)
    await db.commit()
    publish_event(str(run.id), step_completed(run.current_phase or "v2", step))
    publish_event(str(run.id), progress(float(pct), step.replace("_", " ")))
    update_generation_progress(str(run.id), run.current_phase or "v2", step, float(pct))
    config = run.config or {}
    stop_after_step = str(config.get("v2_stop_after_step") or "") if config.get("execution_mode") == "step" else ""
    if stop_after_step == step:
        raise V2StepExecutionStopped(executed_step=step, next_step=next_step)


def phase_completed_event(run_id: str, phase: str) -> None:
    publish_event(run_id, phase_completed(phase, int(time.time() * 1000)))


async def _save_claim_bundle(
    db: AsyncSession,
    run: GenerationRun,
    bundle: ClaimCitationBundle,
    *,
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    model: str | None = None,
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
            model=model,
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
            model=model,
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
            model=model,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_ids.append(str(artifact.id))
    return artifact_ids


async def _validate_and_save_claim_bundle(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    bundle: ClaimCitationBundle,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    run_view: RunCorpusView,
    evidence_tools,
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    pass_key: str,
    metadata: dict | None = None,
) -> tuple[ClaimCitationBundle, list[str], CitationValidationReport, str]:
    evidence_records = await list_evidence_dicts(db, run.id)
    linked_evidence_ids = [link.evidence_id for link in bundle.citation_links if link.evidence_id]
    evidence_artifact_ids = await _save_evidence_artifacts(
        db,
        run,
        evidence_ids=linked_evidence_ids,
        input_artifact_ids=input_artifact_ids,
    )
    support_bundle = await judge_claim_support(
        db,
        run,
        runtime,
        bundle=bundle,
        evidence_records=evidence_records,
        evidence_tools=evidence_tools,
    )
    support_artifact_ids = await _save_support_judgments(
        db,
        run,
        support_bundle,
        input_artifact_ids=[*input_artifact_ids, *evidence_artifact_ids],
        prompt_hash=runtime.prompt_hash_for("v2_fact_citation", "support_judge"),
        model=runtime.model_for("v2_fact_citation", "support_judge"),
        metadata=runtime.fallback_metadata_for("v2_fact_citation", "support_judge"),
    )
    validation_report = await validate_citations(
        db,
        bundle=bundle,
        source_cards=source_cards,
        quote_cards=quote_cards,
        evidence_records=evidence_records,
        support_judgments=support_bundle.judgments,
        run_view=run_view,
    )
    bundle = merge_validation_issues(bundle, validation_report)
    bundle_artifact_ids = await _save_claim_bundle(
        db,
        run,
        bundle,
        input_artifact_ids=[*input_artifact_ids, *evidence_artifact_ids, *support_artifact_ids],
        prompt_hash=prompt_hash,
        model=runtime.model_for("v2_fact_citation", "citation_linker"),
        metadata=metadata,
    )
    bundle_artifact_ids = [*evidence_artifact_ids, *support_artifact_ids, *bundle_artifact_ids]
    validation_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.CITATION_VALIDATION,
        artifact_key=pass_key,
        payload=validation_report,
        status=V2ArtifactStatus.PASSED if validation_report.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[*input_artifact_ids, *bundle_artifact_ids],
        model=runtime.model_for("v2_fact_citation", "citation_linker"),
        prompt_hash=prompt_hash,
        metadata=metadata,
    )
    return bundle, bundle_artifact_ids, validation_report, str(validation_artifact.id)


async def _save_support_judgments(
    db: AsyncSession,
    run: GenerationRun,
    support_bundle: SupportJudgmentBundle,
    *,
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    model: str | None = None,
    metadata: dict | None = None,
) -> list[str]:
    artifact_ids: list[str] = []
    for idx, judgment in enumerate(support_bundle.judgments):
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SUPPORT_JUDGMENT,
            artifact_key=f"{judgment.claim_id}:{judgment.evidence_id}:{idx}",
            payload=judgment,
            status=V2ArtifactStatus.PASSED if judgment.supported and not judgment.blocking else V2ArtifactStatus.FAILED,
            input_artifact_ids=input_artifact_ids,
            model=model,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_ids.append(str(artifact.id))
    return artifact_ids


async def _save_evidence_artifacts(
    db: AsyncSession,
    run: GenerationRun,
    *,
    evidence_ids: list[str],
    input_artifact_ids: list[str],
) -> list[str]:
    return list(
        (
            await _save_evidence_artifact_map(
                db,
                run,
                evidence_ids=evidence_ids,
                input_artifact_ids=input_artifact_ids,
            )
        ).values()
    )


async def _save_evidence_artifact_map(
    db: AsyncSession,
    run: GenerationRun,
    *,
    evidence_ids: list[str],
    input_artifact_ids: list[str],
) -> dict[str, str]:
    if not evidence_ids:
        return {}
    evidence_by_id = {item["id"]: item for item in await list_evidence_dicts(db, run.id)}
    artifact_ids_by_evidence_id: dict[str, str] = {}
    for evidence_id in _dedupe_strings(evidence_ids):
        payload = evidence_by_id.get(evidence_id)
        if not payload:
            continue
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.EVIDENCE_ITEM,
            artifact_key=evidence_id,
            payload=payload,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=input_artifact_ids,
        )
        artifact_ids_by_evidence_id[evidence_id] = str(artifact.id)
    return artifact_ids_by_evidence_id


def _research_memo_evidence_ids(memo: ResearchMemo) -> list[str]:
    return _dedupe_strings(
        evidence_id
        for atom in memo.editorial_atoms
        for evidence_id in atom.evidence_ids
        if evidence_id
    )


def _dedupe_strings(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _merge_source_cards(base_cards: list[SourceCard], override_cards: list[SourceCard]) -> list[SourceCard]:
    overrides_by_source_id = {card.source_id: card for card in override_cards}
    merged: list[SourceCard] = []
    seen: set[str] = set()
    for card in base_cards:
        merged_card = overrides_by_source_id.get(card.source_id, card)
        merged.append(merged_card)
        seen.add(merged_card.source_id)
    for card in override_cards:
        if card.source_id not in seen:
            merged.append(card)
            seen.add(card.source_id)
    return merged


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
        if duration_seconds >= 3600 and len(act.required_evidence_ids) < max(1, min(act.source_density_target, 2)):
            issues.append(f"Act {act.act_number} lacks enough required evidence ids for long-form drafting.")
    return EpisodeCritique(
        passed=not issues,
        summary="Treatment gate passed." if not issues else "Treatment gate failed.",
        weak_sections=issues,
        rewrite_instructions=issues,
    )


def _validate_research_memo_evidence(
    memo: ResearchMemo,
    *,
    evidence_records: list[dict],
    run_view: RunCorpusView,
    duration_seconds: int,
    source_map: SourceMap | None = None,
) -> ResearchAudit:
    valid_source_ids = {str(source_id) for source_id in run_view.selected_source_ids}
    valid_version_ids = {str(version_id) for version_id in run_view.selected_source_version_ids}
    evidence_by_id = {str(item.get("id")): item for item in evidence_records if item.get("id")}
    valid_evidence_ids = {
        evidence_id
        for evidence_id, item in evidence_by_id.items()
        if (item.get("metadata") or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
        and str(item.get("source_id")) in valid_source_ids
        and str(item.get("source_version_id")) in valid_version_ids
    }
    high_impact = {"conflict", "mechanism", "turning_point", "consequence", "human_cost", "institutional_incentive"}
    issues: list[str] = []
    for idx, atom in enumerate(memo.editorial_atoms, start=1):
        invalid_sources = [source_id for source_id in atom.source_ids if source_id not in valid_source_ids]
        if invalid_sources:
            issues.append(f"Atom {idx} references unknown source ids: {', '.join(invalid_sources)}.")
        for evidence_id in atom.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                issues.append(f"Atom {idx} references unknown evidence id: {evidence_id}.")
                continue
            if evidence_id not in valid_evidence_ids:
                metadata = evidence.get("metadata") or {}
                if metadata.get("usage_status") not in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}:
                    issues.append(f"Atom {idx} references evidence id that was not explicitly selected: {evidence_id}.")
                elif str(evidence.get("source_id")) not in valid_source_ids or str(evidence.get("source_version_id")) not in valid_version_ids:
                    issues.append(f"Atom {idx} references evidence id outside the run corpus view: {evidence_id}.")
                else:
                    issues.append(f"Atom {idx} references invalid evidence id: {evidence_id}.")
        if atom.kind in high_impact and not atom.evidence_ids:
            issues.append(f"High-impact atom {idx} ({atom.kind}) has no evidence ids.")
        if atom.source_coverage_status == "weak" and atom.kind in high_impact:
            issues.append(f"High-impact atom {idx} ({atom.kind}) is marked weak.")
    if duration_seconds >= 3600:
        grounded_high_impact = [
            atom
            for atom in memo.editorial_atoms
            if atom.kind in high_impact and atom.evidence_ids and not [eid for eid in atom.evidence_ids if eid not in valid_evidence_ids]
        ]
        if len(grounded_high_impact) < 4:
            issues.append("Long-form V2 memo requires at least four grounded high-impact editorial atoms.")
    if duration_seconds >= 2700:
        coverage = _research_story_coverage(memo, valid_evidence_ids)
        required = {
            "mechanism": "45+ minute memo requires a grounded mechanism atom.",
            "history_origin": "45+ minute memo requires grounded origin/history material.",
            "turning_point": "45+ minute memo requires a grounded turning-point atom.",
            "consequence_tradeoff": "45+ minute memo requires grounded consequence/tradeoff material.",
            "incentive_conflict": "45+ minute memo requires grounded incentive or conflict material.",
            "unanswered_risk": "45+ minute memo requires an unanswered question or explicit risk.",
        }
        for key, message in required.items():
            if not coverage.get(key):
                issues.append(message)
    if source_map is not None:
        cluster_issues = _research_source_map_cluster_coverage_issues(memo, source_map)
        issues.extend(cluster_issues)
    score = 1.0 if not issues else max(0.0, 1.0 - len(issues) * 0.15)
    return ResearchAudit(
        passed=not issues,
        score=score,
        verdict="Research memo evidence gate passed." if not issues else "Research memo evidence gate failed.",
        missing_material=issues,
        required_followups=issues,
    )


def _research_story_coverage(memo: ResearchMemo, valid_evidence_ids: set[str]) -> dict[str, bool]:
    def grounded(atom: EditorialAtom) -> bool:
        return bool(atom.evidence_ids) and all(evidence_id in valid_evidence_ids for evidence_id in atom.evidence_ids)

    coverage = {
        "mechanism": False,
        "history_origin": False,
        "turning_point": False,
        "consequence_tradeoff": False,
        "incentive_conflict": False,
        "unanswered_risk": bool(memo.open_questions or memo.risk_register),
    }
    for atom in memo.editorial_atoms:
        text = f"{atom.title} {atom.explanation} {atom.story_value}".lower()
        if atom.kind == "mechanism" and grounded(atom):
            coverage["mechanism"] = True
        if (atom.kind == "history" or "origin" in text or "history" in text) and grounded(atom):
            coverage["history_origin"] = True
        if atom.kind == "turning_point" and grounded(atom):
            coverage["turning_point"] = True
        if (atom.kind == "consequence" or "tradeoff" in text or "trade-off" in text) and grounded(atom):
            coverage["consequence_tradeoff"] = True
        if atom.kind in {"institutional_incentive", "conflict"} and grounded(atom):
            coverage["incentive_conflict"] = True
        if atom.kind == "unanswered_question":
            coverage["unanswered_risk"] = True
    return coverage


def _research_source_map_cluster_coverage_issues(memo: ResearchMemo, source_map: SourceMap) -> list[str]:
    issues: list[str] = []
    if not source_map.source_triage.source_clusters:
        return issues
    risk_text = " ".join([*memo.open_questions, *memo.risk_register]).lower()
    atom_source_sets = [set(str(source_id) for source_id in atom.source_ids) for atom in memo.editorial_atoms]
    for cluster in source_map.source_triage.source_clusters:
        if not _research_cluster_is_major(cluster):
            continue
        cluster_sources = set(str(source_id) for source_id in cluster.source_ids)
        if not cluster_sources:
            continue
        if any(cluster_sources & source_ids for source_ids in atom_source_sets):
            continue
        label = str(cluster.label or cluster.cluster_id or "source cluster")
        cluster_id = str(cluster.cluster_id or "")
        if (label and label.lower() in risk_text) or (cluster_id and cluster_id.lower() in risk_text):
            continue
        issues.append(f"SourceMap cluster not covered by any research atom or explicit risk: {label}.")
    return issues


def _research_cluster_is_major(cluster: Any) -> bool:
    text = f"{getattr(cluster, 'cluster_id', '')} {getattr(cluster, 'label', '')} {getattr(cluster, 'rationale', '')}".lower()
    low_priority_terms = {
        "duplicate",
        "near-duplicate",
        "near duplicate",
        "mirror",
        "reprint",
        "noise",
        "noisy",
        "weak",
        "low-priority",
        "low priority",
        "deferred",
        "background",
        "supplemental",
    }
    if any(term in text for term in low_priority_terms):
        return False
    source_count = len(getattr(cluster, "source_ids", None) or [])
    return bool(
        source_count >= 2
        or getattr(cluster, "primary_source_ids", None)
        or getattr(cluster, "evidence_ids", None)
    )


def _validate_dossier(dossier: ActDossier, source_cards: list[SourceCard]) -> EpisodeCritique:
    known_sources = {card.source_id for card in source_cards if card.coverage_status in {"ready", "partial"}}
    issues = []
    if len(set(dossier.must_use_sources) & known_sources) < dossier.source_density_minimum:
        issues.append("Act dossier does not meet source density minimum.")
    if len(dossier.evidence_ids) < max(1, dossier.source_density_minimum):
        issues.append("Act dossier does not meet evidence density minimum.")
    if not dossier.quote_cards and not dossier.anchor_refs and not dossier.evidence_ids:
        issues.append("Act dossier has no quote cards, anchor refs, or evidence ids.")
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
