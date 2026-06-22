from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.prompting import (
    ACT_DRAFT_PROMPT,
    ACT_WRITER_SYSTEM,
    ACT_REWRITE_PROMPT,
    ACT_REWRITER_SYSTEM,
    CLAIM_CITATION_PROMPT,
    CITATION_LINKER_SYSTEM,
    EPISODE_CRITIQUE_PROMPT,
    EPISODE_CRITIC_SYSTEM,
    RESEARCH_AUDIT_PROMPT,
    RESEARCH_AUDIT_SYSTEM,
    RESEARCH_MEMO_PROMPT,
    RESEARCH_SYSTEM,
    SOURCE_ACQUISITION_PROMPT,
    SOURCE_ACQUISITION_SYSTEM,
    SOURCE_TRIAGE_FINALIZER_PROMPT,
    SOURCE_TRIAGE_PLANNER_PROMPT,
    SOURCE_TRIAGE_PROMPT,
    SOURCE_TRIAGE_REPAIR_PROMPT,
    SOURCE_TRIAGE_SET_PROMPT,
    SOURCE_TRIAGE_SYSTEM,
    SUPPORT_JUDGMENT_PROMPT,
    SUPPORT_JUDGE_SYSTEM,
    TREATMENT_PROMPT,
    TREATMENT_SYSTEM,
)
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.source_triage import quote_ref, select_sources_for_act
from app.generation_v2.tools.evidence_refs import (
    SourceRefContext,
    build_source_ref_context,
    source_ref_for_id,
)
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    ClaimCitationBundle,
    ContinuityLedger,
    DialogueLine,
    DraftClaim,
    EditorialAtom,
    EpisodeCritique,
    FactIssue,
    FinalTranscript,
    CorpusGapReport,
    ResearchAudit,
    ResearchMemo,
    ShowBibleContext,
    SourceCard,
    SourceAcquisitionCompletion,
    SourceMap,
    SourceTriageCompletion,
    SourceTriageReport,
    QuoteCard,
    CitationLink,
    SupportJudgmentBundle,
    Treatment,
    TreatmentAct,
)
from app.models.generation_run import GenerationRun


TRIAGE_TOOL_NAMES = {
    "list_sources",
    "search_corpus",
    "read_evidence",
    "read_source_window",
    "skim_source",
    "inspect_source",
    "compare_source_evidence",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_source_cluster",
    "record_gap",
    "finalize_source_map",
}
TRIAGE_PLANNER_TOOL_NAMES = {
    "list_source_capsules",
    "upsert_triage_source_set",
    "finalize_triage_workspace_plan",
}
TRIAGE_SET_TOOL_NAMES = {
    "search_corpus",
    "read_evidence",
    "read_source_window",
    "skim_source",
    "inspect_source",
    "compare_source_evidence",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_source_cluster",
    "record_gap",
    "record_triage_observation",
    "finish_triage_source_set",
}
TRIAGE_FINALIZER_TOOL_NAMES = {
    "record_gap",
    "finalize_source_map",
    "finish_triage_source_set",
}
RESEARCH_TOOL_NAMES = {
    "list_sources",
    "search_sources",
    "search_corpus",
    "find_quotes",
    "read_evidence",
    "read_source_window",
    "read_source_with_model",
    "search_within_source",
    "compare_source_evidence",
    "select_evidence",
}
AUDIT_TOOL_NAMES = {"search_corpus", "read_evidence"}
TREATMENT_TOOL_NAMES = {"read_evidence", "read_source_window", "search_within_source"}
WRITER_TOOL_NAMES = {"read_evidence", "read_source_window", "search_within_source"}
CRITIC_TOOL_NAMES = {"search_corpus", "read_evidence"}
REWRITER_TOOL_NAMES = {"read_evidence", "read_source_window", "search_within_source"}
CITATION_TOOL_NAMES = {
    "search_corpus",
    "read_evidence",
    "find_quotes",
    "read_source_window",
    "read_source_with_model",
    "search_within_source",
    "select_evidence",
}
ACQUISITION_TOOL_NAMES = {
    "ingest_source_candidate",
    "reject_source_candidate",
    "finalize_source_acquisition_round",
}


def _phase_tools(evidence_tools: list[AgentTool] | None, allowed_names: set[str]) -> list[AgentTool]:
    return [tool for tool in evidence_tools or [] if tool.name in allowed_names]


def _source_ref_context(source_cards: list[SourceCard]) -> SourceRefContext:
    return build_source_ref_context(SourceMap(source_cards=source_cards))


def _source_inventory(
    source_cards: list[SourceCard],
    *,
    limit: int = 80,
    ref_context: SourceRefContext | None = None,
) -> list[dict]:
    context = ref_context or _source_ref_context(source_cards)
    return [
        {
            "source_ref": source_ref_for_id(card.source_id, context),
            "title": card.title,
            "source_type": card.source_type,
            "domain": card.domain,
            "published_at": card.published_at,
            "length_hint": card.length_hint,
            "coverage_status": card.coverage_status,
            "version_count": len(card.source_version_ids or card.representation_ids or []),
            "ready_version_count": card.ready_representation_count,
            "failed_version_count": card.failed_representation_count,
            "representation_summary": _trim(card.representation_summary, 180),
            "useful_for": card.useful_for[:8],
            "evidence_ids": card.evidence_ids[:8],
            "brief_hint": _trim(card.summary, 420),
            "cautions": card.gaps_or_cautions[:6],
        }
        for card in source_cards[:limit]
    ]


def _count_by(source_cards: list[SourceCard], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for card in source_cards:
        value = str(getattr(card, attr, None) or "unknown").lower()
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _source_inventory_summary(source_cards: list[SourceCard]) -> dict:
    return {
        "total_sources": len(source_cards),
        "list_sources_page_size": 25,
        "source_type_counts": _count_by(source_cards, "source_type"),
        "coverage_status_counts": _count_by(source_cards, "coverage_status"),
        "instruction": "Call list_sources with limit=25 and cursor='' to see source_ref handles and compact source metadata. Continue from next_cursor until it is empty.",
    }


def _quote_inventory(
    quote_cards: list[QuoteCard],
    *,
    limit: int = 80,
    ref_context: SourceRefContext | None = None,
) -> list[dict]:
    return [
        {
            "quote_ref": quote_ref(card),
            "source_ref": source_ref_for_id(card.source_id, ref_context) if ref_context else None,
            "evidence_id": card.evidence_id,
            "locator": card.locator,
            "narrative_use": card.narrative_use,
            "confidence": card.confidence,
            "source_kind": card.source_kind,
            "quote_excerpt": _trim(card.quote, 500),
        }
        for card in quote_cards[:limit]
    ]


def _source_refs(source_ids: list[str], context: SourceRefContext | None) -> list[str]:
    if context is None:
        return ["S??" for source_id in source_ids if source_id]
    return [source_ref_for_id(str(source_id), context) for source_id in source_ids if source_id]


def _triage_inventory(
    source_triage: SourceTriageReport | None,
    *,
    source_limit: int = 40,
    ref_context: SourceRefContext | None = None,
) -> dict:
    triage = source_triage or SourceTriageReport()
    return {
        "ranked_sources": [
            {
                "source_ref": source_ref_for_id(item.source_id, ref_context) if ref_context else "S??",
                "rank": item.rank,
                "score": item.score,
                "useful_for": item.useful_for[:8],
                "evidence_ids": item.evidence_ids[:8],
                "top_quote_refs": item.top_quote_refs[:8],
                "relevance_reasons": item.relevance_reasons[:5],
                "cautions": item.cautions[:5],
            }
            for item in triage.ranked_sources[:source_limit]
        ],
        "top_sources_by_function": {
            key: _source_refs(value[:10], ref_context) for key, value in triage.top_sources_by_function.items()
        },
        "top_evidence_by_function": {key: value[:10] for key, value in triage.top_evidence_by_function.items()},
        "gaps": triage.gaps[:12],
        "contradictions": triage.contradictions[:12],
        "source_clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "label": cluster.label,
                "source_refs": _source_refs(cluster.source_ids, ref_context),
                "primary_source_refs": _source_refs(cluster.primary_source_ids, ref_context),
                "secondary_source_refs": _source_refs(cluster.secondary_source_ids, ref_context),
                "duplicate_source_refs": _source_refs(cluster.duplicate_source_ids, ref_context),
                "evidence_ids": cluster.evidence_ids[:10],
                "rationale": cluster.rationale,
            }
            for cluster in triage.source_clusters[:20]
        ],
        "source_relationships": [
            {
                "source_ref": source_ref_for_id(rel.source_id, ref_context) if ref_context else "S??",
                "related_source_ref": source_ref_for_id(rel.related_source_id, ref_context) if ref_context else "S??",
                "relationship": rel.relationship,
                "evidence_ids": rel.evidence_ids[:10],
                "rationale": rel.rationale,
            }
            for rel in triage.source_relationships[:40]
        ],
    }


def _evidence_inventory(
    evidence_records: list[dict] | None,
    *,
    source_cards: list[SourceCard] | None = None,
    quote_cards: list[QuoteCard] | None = None,
    dossiers: list[ActDossier] | None = None,
    limit: int = 80,
    ref_context: SourceRefContext | None = None,
) -> list[dict]:
    records = evidence_records or []
    by_id = {str(item.get("id")): item for item in records if item.get("id")}
    preferred_ids: list[str] = []
    for card in source_cards or []:
        preferred_ids.extend(card.evidence_ids[:8])
    for quote in quote_cards or []:
        if quote.evidence_id:
            preferred_ids.append(quote.evidence_id)
    for dossier in dossiers or []:
        preferred_ids.extend(dossier.evidence_ids[:12])
        for quote in dossier.quote_cards[:8]:
            if quote.evidence_id:
                preferred_ids.append(quote.evidence_id)

    selected: list[dict] = []
    seen: set[str] = set()
    for evidence_id in preferred_ids:
        item = by_id.get(str(evidence_id))
        if item and str(evidence_id) not in seen:
            selected.append(item)
            seen.add(str(evidence_id))
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        remainder = sorted(
            [item for item in records if str(item.get("id")) not in seen],
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )
        selected.extend(remainder[: limit - len(selected)])

    return [
        {
            "evidence_id": str(item.get("id")),
            "source_ref": (
                source_ref_for_id(str(item.get("source_id")), ref_context)
                if ref_context and item.get("source_id")
                else None
            ),
            "score": item.get("score"),
            "purpose": item.get("purpose"),
            "locator_text": item.get("locator_text"),
            "locator_type": item.get("locator_type"),
            "snippet_text": _trim(str(item.get("snippet_text") or ""), 900),
            "metadata_flags": _evidence_metadata_flags(item.get("metadata") or {}),
        }
        for item in selected[:limit]
    ]


def _evidence_metadata_flags(metadata: dict) -> dict:
    keys = {
        "supports_query",
        "support_level",
        "selected_role",
        "selected_purpose",
    }
    return {key: metadata[key] for key in keys if key in metadata}


def _trim(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _quote_matches_requirements(quote: QuoteCard, refs: set[str]) -> bool:
    return bool(
        (quote.evidence_id and quote.evidence_id in refs)
        or quote_ref(quote) in refs
        or quote.locator in refs
    )


async def build_source_triage_planner_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    workspace_summary: dict,
    workspace_tools: list[AgentTool] | None = None,
) -> SourceTriageCompletion:
    prompt = SOURCE_TRIAGE_PLANNER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        workspace_json=_json(workspace_summary),
    )
    agent_name = "source_triage_planner_agent"
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_source_triage",
            agent_name=agent_name,
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(workspace_tools, TRIAGE_PLANNER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=6000,
            max_tool_rounds=settings.generation_v2_triage_max_tool_rounds or 80,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_triage", agent_name, exc)
        return SourceTriageCompletion(
            ready=False,
            summary="Runtime fallback was used; source triage planning did not run.",
            notes=[],
        )


async def build_source_triage_set_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_set_payload: dict,
    set_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    hosted_file_search_handler=None,
) -> SourceTriageCompletion:
    prompt = SOURCE_TRIAGE_SET_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        source_set_json=_json(source_set_payload),
    )
    set_id = ((source_set_payload or {}).get("set") or {}).get("set_id") or "unknown"
    agent_name = f"source_triage_set_agent_{str(set_id)[:48]}"
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_source_triage",
            agent_name=agent_name,
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_SYSTEM,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_phase_tools(set_tools, TRIAGE_SET_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            hosted_file_search_handler=hosted_file_search_handler,
            max_output_tokens=8000,
            max_tool_rounds=settings.generation_v2_triage_max_tool_rounds or 120,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_triage", agent_name, exc)
        return SourceTriageCompletion(
            ready=False,
            summary=f"Runtime fallback was used; source set {set_id} was not mapped.",
            notes=[],
        )


async def build_source_triage_finalizer_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    workspace_summary: dict,
    source_map: SourceMap,
    finalizer_tools: list[AgentTool] | None = None,
) -> SourceTriageCompletion:
    prompt = SOURCE_TRIAGE_FINALIZER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        workspace_json=_json(workspace_summary),
        source_map_json=_json(source_map.model_dump(mode="json")),
    )
    agent_name = "source_triage_finalizer_agent"
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_source_triage",
            agent_name=agent_name,
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(finalizer_tools, TRIAGE_FINALIZER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=6000,
            max_tool_rounds=24,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_triage", agent_name, exc)
        return SourceTriageCompletion(
            ready=False,
            summary="Runtime fallback was used; source triage finalizer did not run.",
            notes=[],
        )


async def build_source_triage_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_inventory: SourceMap,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    hosted_file_search_handler=None,
    repair_context: dict | None = None,
) -> SourceTriageCompletion:
    user_prompt = (run.config or {}).get("user_prompt", "")
    source_inventory_json = _json(_source_inventory_summary(source_inventory.source_cards))
    if repair_context:
        prompt = SOURCE_TRIAGE_REPAIR_PROMPT.format(
            user_prompt=user_prompt,
            source_inventory_json=source_inventory_json,
            source_map_json=_json(repair_context.get("source_map") or {}),
            gate_json=_json(repair_context.get("gate") or {}),
        )
    else:
        prompt = SOURCE_TRIAGE_PROMPT.format(
            user_prompt=user_prompt,
            source_inventory_json=source_inventory_json,
        )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_source_triage",
            agent_name="source_triage_agent",
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_SYSTEM,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_phase_tools(evidence_tools, TRIAGE_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            hosted_file_search_handler=hosted_file_search_handler,
            max_output_tokens=10000,
            max_tool_rounds=settings.generation_v2_triage_max_tool_rounds,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_triage", "source_triage_agent", exc)
        return SourceTriageCompletion(
            ready=False,
            summary="Runtime fallback was used; source triage write actions were not completed by the model.",
            notes=["Fallback source inventory is not an authoritative SourceMap."],
        )


async def build_source_acquisition_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    gap_report: CorpusGapReport,
    source_inventory: SourceMap | None,
    acquisition_tools: list[AgentTool] | None = None,
    round_number: int,
) -> SourceAcquisitionCompletion:
    source_ref_context = _source_ref_context((source_inventory or SourceMap()).source_cards)
    prompt = SOURCE_ACQUISITION_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        gap_report_json=gap_report.model_dump_json(),
        source_inventory_json=_json(
            _source_inventory(
                (source_inventory or SourceMap()).source_cards,
                limit=120,
                ref_context=source_ref_context,
            )
        ),
        candidate_budget=settings.generation_v2_source_acquisition_candidates_per_round,
        ingest_budget=settings.generation_v2_source_acquisition_max_ingest_per_round,
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_source_acquisition",
            agent_name=f"source_acquisition_round_{round_number}",
            schema_model=SourceAcquisitionCompletion,
            system=SOURCE_ACQUISITION_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(acquisition_tools, ACQUISITION_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=True, local_tools=True),
            max_output_tokens=8000,
            max_tool_rounds=settings.generation_v2_source_acquisition_candidates_per_round + 2,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_acquisition", f"source_acquisition_round_{round_number}", exc)
        return SourceAcquisitionCompletion(
            ready=False,
            summary="Runtime fallback was used; source acquisition did not run.",
            searched_gap_ids=[gap.gap_id for gap in gap_report.gaps],
            notes=["Source acquisition requires OpenAI web_search and is disabled in runtime fallback."],
        )


async def build_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    source_map: SourceMap | None = None,
    source_inventory: SourceMap | None = None,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
    hosted_file_search_handler=None,
) -> ResearchMemo:
    source_map = source_map or SourceMap(source_cards=source_cards, quote_cards=quote_cards)
    full_source_inventory = source_inventory or source_map
    source_ref_context = _source_ref_context(full_source_inventory.source_cards)
    duration_minutes = int((run.config or {}).get("duration_target", 1800)) // 60
    prompt = RESEARCH_MEMO_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=duration_minutes,
        language=(run.config or {}).get("language", "en"),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        full_source_inventory_json=_json(
            _source_inventory(full_source_inventory.source_cards, limit=160, ref_context=source_ref_context)
        ),
        source_map_inventory_json=_json(_source_inventory(source_cards, limit=80, ref_context=source_ref_context)),
        quote_inventory_json=_json(_quote_inventory(quote_cards, limit=60, ref_context=source_ref_context)),
        triage_inventory_json=_json(_triage_inventory(source_map.source_triage, ref_context=source_ref_context)),
    )
    max_tool_rounds = min(120, max(64, len(full_source_inventory.source_cards) * 2 + duration_minutes // 2))
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_editorial_research",
            agent_name="research_agent",
            schema_model=ResearchMemo,
            system=RESEARCH_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(evidence_tools, RESEARCH_TOOL_NAMES),
            tool_policy=ToolPolicy(
                file_search=False,
                web_search=False,
                local_tools=True,
            ),
            max_output_tokens=16000,
            max_tool_rounds=max_tool_rounds,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_editorial_research", "research_agent", exc)
        return _fallback_research_memo(run, source_cards, quote_cards)


async def audit_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap | None = None,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
) -> ResearchAudit:
    source_map = source_map or SourceMap()
    source_ref_context = _source_ref_context(source_map.source_cards)
    prompt = RESEARCH_AUDIT_PROMPT.format(
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        research_memo_json=research_memo.model_dump_json(),
        source_map_json=_json(
            {
                "sources": _source_inventory(source_map.source_cards, limit=80, ref_context=source_ref_context),
                "triage": _triage_inventory(source_map.source_triage, ref_context=source_ref_context),
            }
        ),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_research_audit",
            agent_name="research_auditor",
            schema_model=ResearchAudit,
            system=RESEARCH_AUDIT_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(evidence_tools, AUDIT_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=5000,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_research_audit", "research_auditor", exc)
        atom_count = len(research_memo.editorial_atoms)
        return ResearchAudit(
            passed=atom_count >= 3,
            score=0.8 if atom_count >= 3 else 0.45,
            verdict="Fallback audit: enough editorial atoms for drafting." if atom_count >= 3 else "Not enough editorial atoms.",
            missing_material=[] if atom_count >= 3 else ["More concrete source material is needed."],
            required_followups=[],
        )


async def build_treatment(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap | None = None,
    source_inventory: SourceMap | None = None,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
) -> Treatment:
    source_map = source_map or SourceMap()
    full_source_inventory = source_inventory or source_map
    source_ref_context = _source_ref_context(full_source_inventory.source_cards)
    act_count = _act_count(int((run.config or {}).get("duration_target", 1800)))
    prompt = TREATMENT_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        act_count=act_count,
        research_memo_json=research_memo.model_dump_json(),
        source_map_json=_json(
            {
                "full_sources": _source_inventory(
                    full_source_inventory.source_cards,
                    limit=160,
                    ref_context=source_ref_context,
                ),
                "sources": _source_inventory(source_map.source_cards, limit=80, ref_context=source_ref_context),
                "quotes": _quote_inventory(source_map.quote_cards, limit=60, ref_context=source_ref_context),
                "triage": _triage_inventory(source_map.source_triage, ref_context=source_ref_context),
            }
        ),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_treatment",
            agent_name="story_editor",
            schema_model=Treatment,
            system=TREATMENT_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(evidence_tools, TREATMENT_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=12000,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_treatment", "story_editor", exc)
        return _fallback_treatment(run, research_memo, act_count)


def build_act_dossier(
    *,
    treatment_act: TreatmentAct,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    ledger: ContinuityLedger,
    show_bible: ShowBibleContext | None = None,
    source_triage: SourceTriageReport | None = None,
) -> ActDossier:
    source_density_minimum = max(1, treatment_act.source_density_target)
    act_text = " ".join(
        [
            treatment_act.title,
            treatment_act.story_role,
            treatment_act.opening_question,
            treatment_act.main_turn,
            treatment_act.ending_turn,
        ]
    )
    source_ids = select_sources_for_act(
        source_triage=source_triage,
        preferred_source_ids=treatment_act.source_focus,
        act_text=act_text,
        minimum=source_density_minimum,
    ) or [card.source_id for card in source_cards[:source_density_minimum]]
    quote_refs = set(treatment_act.required_quote_refs)
    selected_quotes = [
        quote
        for quote in quote_cards
        if quote.source_id in source_ids and (not quote_refs or _quote_matches_requirements(quote, quote_refs))
    ]
    if len(selected_quotes) < 4:
        selected_quotes.extend([quote for quote in quote_cards if quote.source_id in source_ids and quote not in selected_quotes])
    selected_quotes = selected_quotes[:8] or quote_cards[:6]
    bible = show_bible or ShowBibleContext()
    evidence_ids = []
    for evidence_id in treatment_act.required_evidence_ids:
        if evidence_id and evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    if source_triage:
        for item in source_triage.ranked_sources:
            if item.source_id in source_ids:
                for evidence_id in item.evidence_ids[:4]:
                    if evidence_id and evidence_id not in evidence_ids:
                        evidence_ids.append(evidence_id)
    for quote in selected_quotes:
        if quote.evidence_id and quote.evidence_id not in evidence_ids:
            evidence_ids.append(quote.evidence_id)
    source_gaps = []
    if len(set(source_ids)) < source_density_minimum:
        source_gaps.append(
            f"Only {len(set(source_ids))} usable source(s) selected for density minimum {source_density_minimum}."
        )
    if len(evidence_ids) < source_density_minimum:
        source_gaps.append(
            f"Only {len(evidence_ids)} evidence anchor(s) selected for density minimum {source_density_minimum}."
        )
    return ActDossier(
        act_number=treatment_act.act_number,
        title=treatment_act.title,
        core_question=treatment_act.opening_question,
        must_use_sources=source_ids[:8],
        quote_cards=selected_quotes,
        anchor_refs=[*treatment_act.required_atom_refs, *treatment_act.required_quote_refs, *evidence_ids],
        evidence_ids=evidence_ids,
        continuity_constraints=[
            *ledger.unresolved_promises[-6:],
            *[f"Avoid repeating phrase: {phrase}" for phrase in ledger.repeated_phrases[-8:]],
        ],
        claim_risks=[f"Verify: {claim}" for claim in ledger.citation_sensitive_claims[-8:]],
        source_gaps=source_gaps,
        forbidden_phrases=bible.forbidden_phrases,
        host_instructions={"hosts": bible.hosts, "tone_and_mood": bible.tone_and_mood, "show_style": bible.show_style},
        source_density_minimum=source_density_minimum,
    )


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
            phase="v2_act_drafting",
            agent_name=f"act_writer_{treatment_act.act_number}",
            schema_model=ActDraft,
            system=ACT_WRITER_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(evidence_tools, WRITER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=18000,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_act_drafting", f"act_writer_{treatment_act.act_number}", exc)
        return _fallback_act_draft(run, treatment_act, dossier)


def update_continuity_ledger(ledger: ContinuityLedger, draft: ActDraft) -> ContinuityLedger:
    text = " ".join(line.text for line in draft.lines)
    repeated = list(ledger.repeated_phrases)
    for phrase in (
        "what matters",
        "the real question",
        "at the same time",
        "it turns out",
        "the point is",
        "here's the thing",
        "in other words",
    ):
        if text.lower().count(phrase) > 1 and phrase not in repeated:
            repeated.append(phrase)
    for phrase in draft.repeated_phrase_watchlist:
        if phrase not in repeated:
            repeated.append(phrase)
    claim_candidates = [
        line.text[:240]
        for line in draft.lines
        if any(
            marker in line.text.lower()
            for marker in ("according to", "report", "study", "data", "source", "shows", "found")
        )
    ][:8]
    return ContinuityLedger(
        introduced_entities=[*ledger.introduced_entities, *draft.used_source_ids[:4]][-40:],
        unresolved_promises=[*ledger.unresolved_promises, f"Pay off act {draft.act_number}: {draft.title}"][-20:],
        used_analogies=ledger.used_analogies[-20:],
        repeated_phrases=repeated[-30:],
        citation_sensitive_claims=[*ledger.citation_sensitive_claims, *claim_candidates][-50:],
    )


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
            phase="v2_critique_rewrite",
            agent_name="episode_critic",
            schema_model=EpisodeCritique,
            system=EPISODE_CRITIC_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(evidence_tools, CRITIC_TOOL_NAMES),
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
            phase="v2_critique_rewrite",
            agent_name=f"act_rewriter_{treatment_act.act_number}",
            schema_model=ActDraft,
            system=ACT_REWRITER_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_phase_tools(evidence_tools, REWRITER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=18000,
        )
        if revised.act_number != treatment_act.act_number:
            revised.act_number = treatment_act.act_number
        return revised
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_critique_rewrite", f"act_rewriter_{treatment_act.act_number}", exc)
        return _fallback_rewrite_act_draft(draft, critique, fact_issues or [])


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
            phase="v2_fact_citation",
            agent_name="citation_linker",
            schema_model=ClaimCitationBundle,
            system=CITATION_LINKER_SYSTEM,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_phase_tools(evidence_tools, CITATION_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=bool(vector_store_id), web_search=False, local_tools=True, max_file_search_results=10),
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
            phase="v2_fact_citation",
            agent_name="support_judge",
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


def final_transcript(
    run: GenerationRun,
    treatment: Treatment,
    drafts: list[ActDraft],
    issues: list[FactIssue],
    bundle: ClaimCitationBundle | None = None,
    source_cards: list[SourceCard] | None = None,
) -> FinalTranscript:
    lines = [line for draft in drafts for line in draft.lines if line.text.strip()]
    claims = bundle.claims if bundle else []
    citation_links = bundle.citation_links if bundle else []
    return FinalTranscript(
        title=treatment.title,
        description=treatment.logline,
        lines=lines,
        quality_metrics={
            "v2_fact_issue_count": len(issues),
            "blocking_fact_issue_count": len([issue for issue in issues if issue.blocking]),
            "remaining_low_fact_issue_count": len([issue for issue in issues if issue.severity == "low"]),
            "supported_claim_count": len([claim for claim in claims if claim.support_level == "supported"]),
            "weak_claim_count": len([claim for claim in claims if claim.support_level == "weak"]),
            "citation_link_count": len(citation_links),
            "source_coverage_status": _source_coverage_status(source_cards or []),
            "v2_dialogue_line_count": len(lines),
            "v2_act_count": len(drafts),
            "verified_fact_pct": 100 if not issues else max(50, 100 - len(issues) * 10),
        },
    )


def _source_coverage_status(source_cards: list[SourceCard]) -> str:
    if not source_cards:
        return "failed"
    if all(card.coverage_status == "ready" for card in source_cards):
        return "ready"
    if any(card.coverage_status in {"ready", "partial"} for card in source_cards):
        return "partial"
    return "failed"


def _allow_runtime_fallback(runtime: V2Runtime, phase: str, agent_name: str, exc: V2RuntimeUnavailable) -> None:
    if not settings.generation_v2_allow_runtime_fallback:
        raise exc
    runtime.mark_fallback_used(phase, agent_name)


def _fallback_research_memo(run: GenerationRun, source_cards: list[SourceCard], quote_cards: list[QuoteCard]) -> ResearchMemo:
    kinds = [
        "conflict",
        "mechanism",
        "history",
        "turning_point",
        "consequence",
        "exception",
        "human_cost",
        "institutional_incentive",
        "unanswered_question",
    ]
    atoms: list[EditorialAtom] = []
    for idx, card in enumerate(source_cards[:9]):
        kind = kinds[idx % len(kinds)]
        atoms.append(
            EditorialAtom(
                kind=kind,  # type: ignore[arg-type]
                title=f"{kind.replace('_', ' ').title()}: {card.title[:90]}",
                explanation=card.summary[:700],
                source_ids=[card.source_id],
                quote_refs=[quote.quote[:120] for quote in quote_cards if quote.source_id == card.source_id][:2],
                story_value=f"Use as {kind.replace('_', ' ')} material in the episode.",
                anchor_refs=[*card.evidence_ids[:3], *[str(ref) for ref in card.representation_ids[:3]]],
                evidence_ids=card.evidence_ids[:4],
                confidence=0.55 if card.coverage_status == "ready" else 0.35,
                risk="low" if card.coverage_status == "ready" else "medium",
                source_coverage_status="ready" if card.coverage_status == "ready" else "partial",
            )
        )
    if not atoms:
        atoms.append(
            EditorialAtom(
                kind="unanswered_question",
                title="Source gap",
                explanation="No project sources were available for evidence-backed research.",
                story_value="Blocks high-confidence editorial generation.",
                confidence=0.0,
                risk="high",
                source_coverage_status="weak",
            )
        )
    title = str((run.config or {}).get("user_prompt") or "Podcast episode")[:120]
    return ResearchMemo(
        title=title,
        thesis=f"The episode should investigate {title} through concrete source-backed turns.",
        narrative_spine="Move from the surface question to mechanisms, stakes, conflict, and consequences.",
        editorial_atoms=atoms,
        source_coverage=[card.title for card in source_cards],
        open_questions=[] if source_cards else ["Add sources before expecting high-quality generation."],
        risk_register=["Fallback memo used because provider runtime was unavailable."],
    )


def _fallback_treatment(run: GenerationRun, memo: ResearchMemo, act_count: int) -> Treatment:
    duration_minutes = int((run.config or {}).get("duration_target", 1800)) / 60
    per_act = max(3.0, duration_minutes / max(1, act_count))
    acts = []
    for idx in range(1, act_count + 1):
        atom = memo.editorial_atoms[(idx - 1) % len(memo.editorial_atoms)]
        acts.append(
            TreatmentAct(
                act_number=idx,
                title=atom.title[:100],
                story_role=atom.kind.replace("_", " "),
                opening_question=f"What does this reveal about {memo.title}?",
                main_turn=atom.explanation[:220],
                ending_turn=f"The next layer changes how we understand {memo.title}.",
                source_focus=atom.source_ids,
                target_minutes=per_act,
                required_atom_refs=atom.anchor_refs,
                required_quote_refs=atom.quote_refs,
                required_evidence_ids=atom.evidence_ids,
                source_density_target=max(1, min(3, len(atom.source_ids) or 1)),
            )
        )
    return Treatment(
        title=memo.title,
        logline=memo.thesis,
        host_dynamic="Host A drives the narrative; Host B challenges assumptions and asks mechanism questions.",
        acts=acts,
        recurring_questions=["What is the mechanism?", "Who pays the price?", "What changes after the turn?"],
        style_constraints=["Avoid generic podcast filler.", "Keep claims source-grounded."],
    )


def _fallback_act_draft(run: GenerationRun, act: TreatmentAct, dossier: ActDossier) -> ActDraft:
    line_count = max(8, min(36, int(act.target_minutes * 2)))
    lines: list[DialogueLine] = []
    source_hint = ", ".join(dossier.must_use_sources[:2]) or "the available source set"
    for idx in range(line_count):
        speaker = "A" if idx % 2 == 0 else "B"
        if idx == 0:
            text = f"Let's open this act with the question: {act.opening_question}"
        elif idx == line_count - 1:
            text = act.ending_turn
        elif speaker == "A":
            text = f"The useful source trail here points to {source_hint}, and the key turn is this: {act.main_turn[:220]}"
        else:
            quote = dossier.quote_cards[idx % len(dossier.quote_cards)].quote if dossier.quote_cards else act.main_turn
            text = f"So the important detail is not just the fact itself, but what it explains: {quote[:240]}"
        lines.append(DialogueLine(speaker=speaker, text=text))
    return ActDraft(
        act_number=act.act_number,
        title=act.title,
        lines=lines,
        used_source_ids=dossier.must_use_sources,
        repeated_phrase_watchlist=[],
    )


def _fallback_rewrite_act_draft(draft: ActDraft, critique: EpisodeCritique, fact_issues: list[FactIssue]) -> ActDraft:
    repair_notes = [issue.repair for issue in fact_issues if issue.act_number == draft.act_number] if fact_issues else []
    if not repair_notes and not critique.rewrite_instructions:
        return draft
    revised_lines = list(draft.lines)
    if repair_notes:
        revised_lines.append(
            DialogueLine(
                speaker="B",
                text="Where the source trail is not strong enough, we should treat this as an open question rather than a settled claim.",
            )
        )
    return ActDraft(
        act_number=draft.act_number,
        title=draft.title,
        lines=revised_lines,
        used_source_ids=draft.used_source_ids,
        repeated_phrase_watchlist=draft.repeated_phrase_watchlist[-20:],
    )


def _fallback_claims(
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    drafts: list[ActDraft],
) -> ClaimCitationBundle:
    claims: list[DraftClaim] = []
    links: list[CitationLink] = []
    issues: list[FactIssue] = []
    for draft in drafts:
        source_id = draft.used_source_ids[0] if draft.used_source_ids else (source_cards[0].source_id if source_cards else "")
        claim_id = f"act-{draft.act_number}-core"
        statement = draft.lines[0].text if draft.lines else draft.title
        quote = next((quote for quote in quote_cards if quote.source_id == source_id), None)
        claims.append(
            DraftClaim(
                claim_id=claim_id,
                statement=statement[:500],
                act_number=draft.act_number,
                line_index=0,
                text_span=statement[:240],
                claim_type="factual",
                source_need="required",
                risk_level="low" if source_id else "high",
                support_level="supported" if source_id else "unsupported",
                evidence_ids=[quote.evidence_id] if quote and quote.evidence_id else [],
            )
        )
        if source_id:
            links.append(
                CitationLink(
                    claim_id=claim_id,
                    source_id=source_id,
                    source_version_id=quote.source_version_id if quote else None,
                    evidence_id=quote.evidence_id if quote else None,
                    quote=quote.quote if quote else None,
                    locator=quote.locator if quote else "act-level source focus",
                    representation_id=quote.representation_id if quote else None,
                    quote_card_ref=quote.locator if quote else None,
                    supports_span=statement[:240],
                    confidence=0.7 if quote and quote.evidence_id else 0.55,
                )
            )
        else:
            issues.append(
                FactIssue(
                    claim_id=claim_id,
                    act_number=draft.act_number,
                    issue="No source is available for this act-level claim.",
                    repair="Add sources or rewrite as an open question.",
                    severity="high",
                    blocking=True,
                )
            )
    return ClaimCitationBundle(claims=claims, citation_links=links, fact_issues=issues)


def _fallback_support_judgments(bundle: ClaimCitationBundle, evidence_by_id: dict[str, dict]) -> SupportJudgmentBundle:
    judgments = []
    claim_by_id = {claim.claim_id: claim for claim in bundle.claims}
    for link in bundle.citation_links:
        evidence = evidence_by_id.get(str(link.evidence_id or ""))
        claim = claim_by_id.get(link.claim_id)
        if not evidence or not claim:
            continue
        snippet = str(evidence.get("snippet_text") or "").lower()
        statement = claim.statement.lower()
        terms = [term for term in statement.split() if len(term) > 5]
        overlap = len([term for term in terms if term in snippet])
        supported = overlap >= max(1, min(3, len(terms) // 4))
        judgments.append(
            {
                "claim_id": claim.claim_id,
                "evidence_id": str(link.evidence_id),
                "supported": supported,
                "support_level": "supported" if supported else "weak",
                "rationale": "Debug fallback lexical overlap judgment.",
                "required_repair": "" if supported else "Verify with a stronger evidence snippet or soften the claim.",
                "blocking": not supported and claim.source_need == "required",
            }
        )
    return SupportJudgmentBundle.model_validate({"judgments": judgments})


def _act_count(duration_seconds: int) -> int:
    minutes = duration_seconds / 60
    if minutes <= 30:
        return 3
    if minutes <= 45:
        return 4
    if minutes < 60:
        return 5
    return 6


def _json(value: object) -> str:
    if isinstance(value, list):
        return json.dumps(
            [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value],
            ensure_ascii=False,
        )
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)
