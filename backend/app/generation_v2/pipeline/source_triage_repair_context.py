from __future__ import annotations

from app.generation_v2.schemas import SourceMap


def source_triage_repair_context(source_map: SourceMap, triage_gate) -> dict:
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
