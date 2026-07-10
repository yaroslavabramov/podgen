from __future__ import annotations

from app.generation_v2.agents.editorial_context import quote_matches_requirements as _quote_matches_requirements
from app.generation_v2.schemas import (
    ActDossier,
    ContinuityLedger,
    QuoteCard,
    ShowBibleContext,
    SourceCard,
    SourceTriageReport,
    TreatmentAct,
)
from app.generation_v2.source_triage import select_sources_for_act


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
    selected_quotes = selected_quotes_for_act(
        source_ids=source_ids,
        required_quote_refs=treatment_act.required_quote_refs,
        quote_cards=quote_cards,
    )
    bible = show_bible or ShowBibleContext()
    evidence_ids = dossier_evidence_ids(
        required_evidence_ids=treatment_act.required_evidence_ids,
        selected_quotes=selected_quotes,
        source_ids=source_ids,
        source_triage=source_triage,
    )
    source_gaps = source_density_gaps(
        source_ids=source_ids,
        evidence_ids=evidence_ids,
        source_density_minimum=source_density_minimum,
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


def selected_quotes_for_act(
    *,
    source_ids: list[str],
    required_quote_refs: list[str],
    quote_cards: list[QuoteCard],
) -> list[QuoteCard]:
    quote_refs = set(required_quote_refs)
    selected_quotes = [
        quote
        for quote in quote_cards
        if quote.source_id in source_ids and (not quote_refs or _quote_matches_requirements(quote, quote_refs))
    ]
    if len(selected_quotes) < 4:
        selected_quotes.extend([quote for quote in quote_cards if quote.source_id in source_ids and quote not in selected_quotes])
    return selected_quotes[:8] or quote_cards[:6]


def dossier_evidence_ids(
    *,
    required_evidence_ids: list[str],
    selected_quotes: list[QuoteCard],
    source_ids: list[str],
    source_triage: SourceTriageReport | None,
) -> list[str]:
    evidence_ids = []
    for evidence_id in required_evidence_ids:
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
    return evidence_ids


def source_density_gaps(*, source_ids: list[str], evidence_ids: list[str], source_density_minimum: int) -> list[str]:
    source_gaps = []
    if len(set(source_ids)) < source_density_minimum:
        source_gaps.append(
            f"Only {len(set(source_ids))} usable source(s) selected for density minimum {source_density_minimum}."
        )
    if len(evidence_ids) < source_density_minimum:
        source_gaps.append(
            f"Only {len(evidence_ids)} evidence anchor(s) selected for density minimum {source_density_minimum}."
        )
    return source_gaps


__all__ = [
    "build_act_dossier",
    "dossier_evidence_ids",
    "selected_quotes_for_act",
    "source_density_gaps",
]
