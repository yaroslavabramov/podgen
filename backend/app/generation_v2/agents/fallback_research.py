from __future__ import annotations

from app.generation_v2.schemas import EditorialAtom, QuoteCard, ResearchMemo, SourceCard
from app.models.generation_run import GenerationRun


def source_coverage_status(source_cards: list[SourceCard]) -> str:
    if not source_cards:
        return "failed"
    if all(card.coverage_status == "ready" for card in source_cards):
        return "ready"
    if any(card.coverage_status in {"ready", "partial"} for card in source_cards):
        return "partial"
    return "failed"


def fallback_research_memo(
    run: GenerationRun,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
) -> ResearchMemo:
    kinds = [
        "tension",
        "mechanism",
        "context",
        "change",
        "impact",
        "exception",
        "driver",
        "open_question",
    ]
    atoms: list[EditorialAtom] = []
    for idx, card in enumerate(source_cards[:9]):
        kind = kinds[idx % len(kinds)]
        atoms.append(
            EditorialAtom(
                kind=kind,  # type: ignore[arg-type]
                title=f"{kind.replace('_', ' ').title()}: {card.title}",
                explanation=card.summary,
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
                kind="open_question",
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
        research_synthesis="Fallback synthesis from available source cards; use only as a low-confidence research map until a full research workflow runs.",
        editorial_atoms=atoms,
        source_coverage=[card.title for card in source_cards],
        open_questions=[] if source_cards else ["Add sources before expecting high-quality generation."],
        risk_register=["Fallback memo used because provider runtime was unavailable."],
    )


__all__ = ["fallback_research_memo", "source_coverage_status"]
