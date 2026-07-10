from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial_context import (
    evidence_inventory as _evidence_inventory,
    quote_inventory as _quote_inventory,
    source_inventory as _source_inventory,
    source_ref_context as _source_ref_context,
)
from app.generation_v2.agents.drafting_context import json_dumps as _json
from app.generation_v2.agents.fallbacks import (
    allow_runtime_fallback as _allow_runtime_fallback,
    fallback_claims as _fallback_claims,
    fallback_support_judgments as _fallback_support_judgments,
)
from app.generation_v2.agents.tool_sets import CITATION_TOOL_NAMES, phase_tools
from app.generation_v2.prompting import (
    CLAIM_CITATION_PROMPT,
    CITATION_LINKER_SYSTEM,
    SUPPORT_JUDGMENT_PROMPT,
    SUPPORT_JUDGE_SYSTEM,
)
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    ClaimCitationBundle,
    EpisodeCritique,
    QuoteCard,
    ShowBibleContext,
    SourceCard,
    SupportJudgmentBundle,
)
from app.models.generation_run import GenerationRun


async def extract_and_link_claims(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    evidence_records: list[dict] | None = None,
    evidence_tools: list[AgentTool] | None = None,
    dossiers: list[ActDossier] | None = None,
    drafts: list[ActDraft],
    critique: EpisodeCritique,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
    hosted_file_search_handler=None,
) -> ClaimCitationBundle:
    source_ref_context = _source_ref_context(source_cards)
    prompt = CLAIM_CITATION_PROMPT.format(
        source_inventory_json=_json(_source_inventory(source_cards, limit=100, ref_context=source_ref_context)),
        quote_inventory_json=_json(_quote_inventory(quote_cards, limit=80, ref_context=source_ref_context)),
        evidence_inventory_json=_json(
            _evidence_inventory(
                evidence_records,
                source_cards=source_cards,
                quote_cards=quote_cards,
                dossiers=dossiers,
                limit=90,
                ref_context=source_ref_context,
            )
        ),
        draft_json=_json(drafts),
        critique_json=critique.model_dump_json(),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.FACT_CITATION,
            agent_name=V2AgentName.CITATION_LINKER,
            schema_model=ClaimCitationBundle,
            system=CITATION_LINKER_SYSTEM,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=phase_tools(evidence_tools, CITATION_TOOL_NAMES),
            tool_policy=ToolPolicy(
                file_search=bool(vector_store_id),
                web_search=False,
                local_tools=True,
                max_file_search_results=10,
            ),
            hosted_file_search_handler=hosted_file_search_handler,
            max_output_tokens=14000,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_fact_citation", "citation_linker", exc)
        return _fallback_claims(source_cards, quote_cards, drafts)


async def judge_claim_support(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    bundle: ClaimCitationBundle,
    evidence_records: list[dict],
    evidence_tools: list[AgentTool],
) -> SupportJudgmentBundle:
    evidence_by_id = {str(item.get("id")): item for item in evidence_records}
    claim_by_id = {claim.claim_id: claim for claim in bundle.claims}
    pairs = []
    for link in bundle.citation_links:
        if not link.evidence_id:
            continue
        evidence = evidence_by_id.get(link.evidence_id)
        claim = claim_by_id.get(link.claim_id)
        if not claim or not evidence:
            continue
        pairs.append(
            {
                "claim": claim.model_dump(mode="json"),
                "citation_link": link.model_dump(mode="json"),
                "evidence": evidence,
            }
        )
    if not pairs:
        return SupportJudgmentBundle()
    prompt = SUPPORT_JUDGMENT_PROMPT.format(pairs_json=_json(pairs))
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.FACT_CITATION,
            agent_name=V2AgentName.SUPPORT_JUDGE,
            schema_model=SupportJudgmentBundle,
            system=SUPPORT_JUDGE_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=[],
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=False),
            max_output_tokens=10000,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_fact_citation", "support_judge", exc)
        return _fallback_support_judgments(bundle, evidence_by_id)
