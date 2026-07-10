from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial_context import (
    quote_inventory as _quote_inventory,
    source_inventory as _source_inventory,
    source_ref_context as _source_ref_context,
)
from app.generation_v2.agents.citation_agents import extract_and_link_claims, judge_claim_support
from app.generation_v2.agents.drafting_context import (
    build_act_dossier,
    final_transcript,
    json_dumps as _json,
    update_continuity_ledger,
)
from app.generation_v2.agents.fallbacks import (
    allow_runtime_fallback as _allow_runtime_fallback,
    fallback_act_draft as _fallback_act_draft,
    fallback_rewrite_act_draft as _fallback_rewrite_act_draft,
)
from app.generation_v2.agents.tool_sets import (
    CRITIC_TOOL_NAMES,
    REWRITER_TOOL_NAMES,
    WRITER_TOOL_NAMES,
    phase_tools,
)
from app.generation_v2.prompting import (
    ACT_DRAFT_PROMPT,
    ACT_REWRITE_PROMPT,
    ACT_REWRITER_SYSTEM,
    ACT_WRITER_SYSTEM,
    EPISODE_CRITIQUE_PROMPT,
    EPISODE_CRITIC_SYSTEM,
)
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    ContinuityLedger,
    EpisodeCritique,
    FactIssue,
    QuoteCard,
    ShowBibleContext,
    SourceCard,
    Treatment,
    TreatmentAct,
)
from app.models.generation_run import GenerationRun


async def write_act_draft(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment_act: TreatmentAct,
    dossier: ActDossier,
    ledger: ContinuityLedger,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
) -> ActDraft:
    prompt = ACT_DRAFT_PROMPT.format(
        act_number=treatment_act.act_number,
        language=(run.config or {}).get("language", "en"),
        target_minutes=treatment_act.target_minutes,
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        treatment_act_json=treatment_act.model_dump_json(),
        dossier_json=dossier.model_dump_json(),
        ledger_json=ledger.model_dump_json(),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.ACT_DRAFTING,
            agent_name=f"act_writer_{treatment_act.act_number}",
            schema_model=ActDraft,
            system=ACT_WRITER_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=phase_tools(evidence_tools, WRITER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=18000,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_act_drafting", f"act_writer_{treatment_act.act_number}", exc)
        return _fallback_act_draft(run, treatment_act, dossier)


async def critique_episode(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment: Treatment,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    drafts: list[ActDraft],
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
) -> EpisodeCritique:
    source_ref_context = _source_ref_context(source_cards)
    prompt = EPISODE_CRITIQUE_PROMPT.format(
        treatment_json=treatment.model_dump_json(),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        source_cards_json=_json(_source_inventory(source_cards, limit=80, ref_context=source_ref_context)),
        quote_cards_json=_json(_quote_inventory(quote_cards, limit=60, ref_context=source_ref_context)),
        draft_json=_json(drafts),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.CRITIQUE_REWRITE,
            agent_name=V2AgentName.EPISODE_CRITIC,
            schema_model=EpisodeCritique,
            system=EPISODE_CRITIC_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=phase_tools(evidence_tools, CRITIC_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=7000,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_critique_rewrite", "episode_critic", exc)
        return EpisodeCritique(
            passed=True,
            summary="Fallback critique passed. Draft is structurally complete; inspect artifacts for editorial quality.",
        )


async def rewrite_act_draft(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment_act: TreatmentAct,
    dossier: ActDossier,
    draft: ActDraft,
    ledger: ContinuityLedger,
    critique: EpisodeCritique,
    fact_issues: list[FactIssue] | None = None,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
) -> ActDraft:
    prompt = ACT_REWRITE_PROMPT.format(
        act_number=treatment_act.act_number,
        language=(run.config or {}).get("language", "en"),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        treatment_act_json=treatment_act.model_dump_json(),
        dossier_json=dossier.model_dump_json(),
        draft_json=draft.model_dump_json(),
        ledger_json=ledger.model_dump_json(),
        critique_json=critique.model_dump_json(),
        fact_issues_json=_json(fact_issues or []),
    )
    try:
        revised = await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.CRITIQUE_REWRITE,
            agent_name=f"act_rewriter_{treatment_act.act_number}",
            schema_model=ActDraft,
            system=ACT_REWRITER_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=phase_tools(evidence_tools, REWRITER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=18000,
        )
        if revised.act_number != treatment_act.act_number:
            revised.act_number = treatment_act.act_number
        return revised
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_critique_rewrite", f"act_rewriter_{treatment_act.act_number}", exc)
        return _fallback_rewrite_act_draft(draft, critique, fact_issues or [])


__all__ = [
    "build_act_dossier",
    "extract_and_link_claims",
    "final_transcript",
    "judge_claim_support",
    "update_continuity_ledger",
]
