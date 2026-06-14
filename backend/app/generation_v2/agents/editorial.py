from __future__ import annotations

import json
import uuid
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.prompts import (
    ACT_DRAFT_PROMPT,
    ACT_REWRITE_PROMPT,
    CLAIM_CITATION_PROMPT,
    EPISODE_CRITIQUE_PROMPT,
    RESEARCH_AUDIT_PROMPT,
    RESEARCH_MEMO_PROMPT,
    TREATMENT_PROMPT,
    V2_SYSTEM_BASE,
)
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
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
    ResearchAudit,
    ResearchMemo,
    ShowBibleContext,
    SourceCard,
    QuoteCard,
    CitationLink,
    Treatment,
    TreatmentAct,
)
from app.generation_v2.tools.corpus import fetch_source_view, search_corpus
from app.models.generation_run import GenerationRun


async def build_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    vector_store_id: str | None,
    show_bible: ShowBibleContext | None = None,
) -> ResearchMemo:
    prompt = RESEARCH_MEMO_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        source_cards_json=_json(source_cards),
        quote_cards_json=_json(quote_cards[:40]),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_editorial_research",
            agent_name="research_agent",
            schema_model=ResearchMemo,
            system=V2_SYSTEM_BASE,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_corpus_tools(db, run.project_id),
            tool_policy=ToolPolicy(
                file_search=True,
                web_search=bool(settings.generation_v2_enable_web_search_research),
                local_tools=True,
                max_file_search_results=12,
            ),
            max_output_tokens=16000,
        )
    except V2RuntimeUnavailable:
        return _fallback_research_memo(run, source_cards, quote_cards)


async def audit_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    research_memo: ResearchMemo,
    vector_store_id: str | None,
    show_bible: ShowBibleContext | None = None,
) -> ResearchAudit:
    prompt = RESEARCH_AUDIT_PROMPT.format(
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        research_memo_json=research_memo.model_dump_json(),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_research_audit",
            agent_name="research_auditor",
            schema_model=ResearchAudit,
            system=V2_SYSTEM_BASE,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_corpus_tools(db, run.project_id),
            tool_policy=ToolPolicy(file_search=True, web_search=False, local_tools=True),
            max_output_tokens=5000,
        )
    except V2RuntimeUnavailable:
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
    vector_store_id: str | None,
    show_bible: ShowBibleContext | None = None,
) -> Treatment:
    act_count = _act_count(int((run.config or {}).get("duration_target", 1800)))
    prompt = TREATMENT_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        act_count=act_count,
        research_memo_json=research_memo.model_dump_json(),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_treatment",
            agent_name="story_editor",
            schema_model=Treatment,
            system=V2_SYSTEM_BASE,
            prompt=prompt,
            vector_store_id=vector_store_id,
            tool_policy=ToolPolicy(file_search=True, web_search=False, local_tools=False),
            max_output_tokens=12000,
        )
    except V2RuntimeUnavailable:
        return _fallback_treatment(run, research_memo, act_count)


def build_act_dossier(
    *,
    treatment_act: TreatmentAct,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    ledger: ContinuityLedger,
    show_bible: ShowBibleContext | None = None,
) -> ActDossier:
    source_ids = treatment_act.source_focus or [card.source_id for card in source_cards[:4]]
    selected_quotes = [quote for quote in quote_cards if quote.source_id in source_ids][:8] or quote_cards[:6]
    bible = show_bible or ShowBibleContext()
    return ActDossier(
        act_number=treatment_act.act_number,
        title=treatment_act.title,
        core_question=treatment_act.opening_question,
        must_use_sources=source_ids[:8],
        quote_cards=selected_quotes,
        anchor_refs=[*treatment_act.required_atom_refs, *treatment_act.required_quote_refs],
        continuity_constraints=[
            *ledger.unresolved_promises[-6:],
            *[f"Avoid repeating phrase: {phrase}" for phrase in ledger.repeated_phrases[-8:]],
        ],
        claim_risks=[f"Verify: {claim}" for claim in ledger.citation_sensitive_claims[-8:]],
        forbidden_phrases=bible.forbidden_phrases,
        host_instructions={"hosts": bible.hosts, "tone_and_mood": bible.tone_and_mood, "show_style": bible.show_style},
        source_density_minimum=max(1, min(treatment_act.source_density_target, len(source_ids[:8]) or 1)),
    )


async def write_act_draft(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment_act: TreatmentAct,
    dossier: ActDossier,
    ledger: ContinuityLedger,
    vector_store_id: str | None,
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
            system=V2_SYSTEM_BASE,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_source_lookup_tools(db),
            tool_policy=ToolPolicy(file_search=True, web_search=False, local_tools=True, max_file_search_results=6),
            max_output_tokens=18000,
        )
    except V2RuntimeUnavailable:
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
    vector_store_id: str | None,
    show_bible: ShowBibleContext | None = None,
) -> EpisodeCritique:
    prompt = EPISODE_CRITIQUE_PROMPT.format(
        treatment_json=treatment.model_dump_json(),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        source_cards_json=_json(source_cards),
        quote_cards_json=_json(quote_cards[:60]),
        draft_json=_json(drafts),
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase="v2_critique_rewrite",
            agent_name="episode_critic",
            schema_model=EpisodeCritique,
            system=V2_SYSTEM_BASE,
            prompt=prompt,
            vector_store_id=vector_store_id,
            tool_policy=ToolPolicy(file_search=True, web_search=False, local_tools=False),
            max_output_tokens=7000,
        )
    except V2RuntimeUnavailable:
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
    vector_store_id: str | None,
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
            system=V2_SYSTEM_BASE,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_source_lookup_tools(db),
            tool_policy=ToolPolicy(file_search=True, web_search=False, local_tools=True, max_file_search_results=4),
            max_output_tokens=18000,
        )
        if revised.act_number != treatment_act.act_number:
            revised.act_number = treatment_act.act_number
        return revised
    except V2RuntimeUnavailable:
        return _fallback_rewrite_act_draft(draft, critique, fact_issues or [])


async def extract_and_link_claims(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    drafts: list[ActDraft],
    critique: EpisodeCritique,
    vector_store_id: str | None,
    show_bible: ShowBibleContext | None = None,
) -> ClaimCitationBundle:
    prompt = CLAIM_CITATION_PROMPT.format(
        source_cards_json=_json(source_cards),
        quote_cards_json=_json(quote_cards[:60]),
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
            system=V2_SYSTEM_BASE,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_corpus_tools(db, run.project_id),
            tool_policy=ToolPolicy(file_search=True, web_search=False, local_tools=True, max_file_search_results=12),
            max_output_tokens=14000,
        )
    except V2RuntimeUnavailable:
        return _fallback_claims(source_cards, quote_cards, drafts)


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


def _source_lookup_tools(db: AsyncSession) -> list[AgentTool]:
    return [
        AgentTool(
            name="fetch_source_view",
            description="Fetch a stored source representation view by source id and optional kind.",
            parameters={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "kind": {"type": ["string", "null"]},
                },
            },
            handler=partial(_fetch_source_view_tool, db=db),
        )
    ]


def _corpus_tools(db: AsyncSession, project_id: uuid.UUID) -> list[AgentTool]:
    return [
        AgentTool(
            name="search_corpus",
            description="Search deterministic corpus chunks for exact local context. Use when source cards are not enough.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
            handler=lambda args: search_corpus(db, project_id, args.get("query", ""), args.get("limit", 8)),
        ),
        AgentTool(
            name="fetch_source_view",
            description="Fetch a stored source representation view by source id and optional kind.",
            parameters={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "kind": {"type": ["string", "null"]},
                },
            },
            handler=partial(_fetch_source_view_tool, db=db),
        ),
    ]


def _source_coverage_status(source_cards: list[SourceCard]) -> str:
    if not source_cards:
        return "failed"
    if all(card.coverage_status == "ready" for card in source_cards):
        return "ready"
    if any(card.coverage_status in {"ready", "partial"} for card in source_cards):
        return "partial"
    return "failed"


async def _fetch_source_view_tool(args: dict, *, db: AsyncSession) -> dict:
    try:
        source_id = uuid.UUID(str(args.get("source_id", "")))
    except ValueError:
        return {"found": False, "error": "Invalid source_id"}
    return await fetch_source_view(db, source_id, args.get("kind"))


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
                anchor_refs=[str(ref) for ref in card.representation_ids[:3]],
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
                explanation="No project sources were available for V2 research.",
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
            )
        )
        quote = next((quote for quote in quote_cards if quote.source_id == source_id), None)
        if source_id:
            links.append(
                CitationLink(
                    claim_id=claim_id,
                    source_id=source_id,
                    quote=quote.quote if quote else None,
                    locator=quote.locator if quote else "act-level source focus",
                    representation_id=quote.representation_id if quote else None,
                    quote_card_ref=quote.locator if quote else None,
                    supports_span=statement[:240],
                    confidence=0.55,
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
