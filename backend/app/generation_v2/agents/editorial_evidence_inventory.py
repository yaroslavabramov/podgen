from __future__ import annotations

from app.generation_v2.schemas import ActDossier, QuoteCard, SourceCard
from app.generation_v2.tools.evidence_refs import SourceRefContext, source_ref_for_id


def evidence_inventory(
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
            "snippet_text": trim(str(item.get("snippet_text") or ""), 900),
            "metadata_flags": evidence_metadata_flags(item.get("metadata") or {}),
        }
        for item in selected[:limit]
    ]


def evidence_metadata_flags(metadata: dict) -> dict:
    keys = {
        "supports_query",
        "support_level",
        "selected_role",
        "selected_purpose",
    }
    return {key: metadata[key] for key in keys if key in metadata}


def trim(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


__all__ = ["evidence_inventory", "evidence_metadata_flags", "trim"]
