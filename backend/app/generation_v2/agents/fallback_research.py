from __future__ import annotations

from app.generation_v2.schemas import EditorialAtom, QuoteCard, ResearchMemo, SourceCard
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_ANCHOR_REF_LIMIT,
    RESEARCH_QUOTE_REF_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
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
    for idx, card in enumerate(source_cards):
        kind = kinds[idx % len(kinds)]
        atom_title = f"{kind.replace('_', ' ').title()}: {card.title}"
        _validate_semantic_field("title", atom_title)
        _validate_semantic_field("claim", card.summary)
        quote_refs = [
            str(quote.evidence_id)
            for quote in quote_cards
            if quote.source_id == card.source_id
            and quote.evidence_id
            and str(quote.evidence_id) in set(card.evidence_ids)
        ]
        for quote_ref in quote_refs:
            _validate_semantic_field("quote_ref", quote_ref)
        anchor_refs = [*card.evidence_ids, *[str(ref) for ref in card.representation_ids]]
        _validate_semantic_list("quote_refs", quote_refs, RESEARCH_QUOTE_REF_LIMIT)
        _validate_semantic_list("anchor_refs", anchor_refs, RESEARCH_ANCHOR_REF_LIMIT)
        atoms.append(
            EditorialAtom(
                kind=kind,  # type: ignore[arg-type]
                title=atom_title,
                claim=card.summary,
                explanation=card.summary,
                source_ids=[card.source_id],
                quote_refs=quote_refs,
                story_value=f"Use as {kind.replace('_', ' ')} material in the episode.",
                anchor_refs=anchor_refs,
                evidence_ids=card.evidence_ids,
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
                claim="No project sources were available for evidence-backed research.",
                explanation="No project sources were available for evidence-backed research.",
                story_value="Blocks high-confidence editorial generation.",
                confidence=0.0,
                risk="high",
                source_coverage_status="weak",
            )
        )
    title = str((run.config or {}).get("user_prompt") or "Podcast episode")
    _validate_semantic_field("title", title)
    return ResearchMemo(
        title=title,
        thesis=f"The episode should investigate {title} through concrete source-backed turns.",
        research_synthesis="Fallback synthesis from available source cards; use only as a low-confidence research map until a full research workflow runs.",
        editorial_atoms=atoms,
        source_coverage=[card.title for card in source_cards],
        open_questions=[] if source_cards else ["Add sources before expecting high-quality generation."],
        risk_register=["Fallback memo used because provider runtime was unavailable."],
    )


def _validate_semantic_field(field: str, value: str) -> None:
    limit = SEMANTIC_FIELD_LIMITS[field]
    if len(value) > limit:
        raise ValueError(
            f"semantic_field_too_long: field={field}, max_length={limit}, actual_length={len(value)}"
        )


def _validate_semantic_list(field: str, values: list[str], limit: int) -> None:
    if len(values) > limit:
        raise ValueError(
            f"semantic_list_too_long: field={field}, max_items={limit}, actual_items={len(values)}"
        )


__all__ = ["fallback_research_memo", "source_coverage_status"]
