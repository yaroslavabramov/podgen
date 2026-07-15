from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.generation_v2.pipeline.state import triage_runtime_values
from app.generation_v2.pipeline.validation import merge_source_cards as _merge_source_cards


@dataclass(frozen=True)
class EditorialResearchResult:
    triage_state: Any
    acquisition_rounds_used: int
    corpus: Any
    evidence_toolset: Any
    evidence_tools: Any
    source_map: Any
    source_cards: list[Any]
    quote_cards: list[Any]
    source_triage: Any
    source_artifact_ids: list[str]
    quote_artifact_ids: list[str]
    source_map_artifact_id: str | None
    source_workspace_artifact_ids: list[str]
    run_source_cards: list[Any]
    treatment: Any
    treatment_artifact_id: str


def editorial_runtime_values(triage_state: Any) -> dict[str, Any]:
    corpus, evidence_toolset, evidence_tools, source_map, source_cards, quote_cards, source_triage = (
        triage_runtime_values(triage_state)
    )
    return {
        "corpus": corpus,
        "evidence_toolset": evidence_toolset,
        "evidence_tools": evidence_tools,
        "source_map": source_map,
        "source_cards": source_cards,
        "quote_cards": quote_cards,
        "source_triage": source_triage,
        "source_artifact_ids": triage_state.source_artifact_ids,
        "quote_artifact_ids": triage_state.quote_artifact_ids,
        "source_map_artifact_id": triage_state.source_map_artifact_id,
        "corpus_brief": triage_state.corpus_brief,
        "corpus_brief_artifact_id": triage_state.corpus_brief_artifact_id,
        "source_workspace_artifact_ids": triage_state.source_workspace_artifact_ids,
        "run_source_cards": _merge_source_cards(triage_state.source_inventory.source_cards, source_cards),
    }


__all__ = ["EditorialResearchResult", "editorial_runtime_values"]
