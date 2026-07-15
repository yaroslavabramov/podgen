from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.generation_v2.schemas import QuoteCard, SourceCard, SourceMap
from app.generation_v2.source_corpus import CorpusPreparationResult
from app.generation_v2.tools.evidence import EvidenceToolset


@dataclass
class TriageState:
    corpus: CorpusPreparationResult
    evidence_toolset: EvidenceToolset
    evidence_tools: list
    source_inventory: SourceMap
    triage_completion: object
    source_map: SourceMap
    triage_gate: object
    source_cards: list[SourceCard]
    quote_cards: list[QuoteCard]
    source_map_artifact_id: str
    evidence_artifact_ids: list[str]
    source_artifact_ids: list[str]
    quote_artifact_ids: list[str]
    workspace_artifact_id: str | None
    triage_artifact_id: str
    triage_gate_artifact_id: str
    corpus_brief: object | None = None
    corpus_brief_artifact_id: str | None = None
    corpus_brief_audit_artifact_id: str | None = None
    corpus_brief_shadow_audit_artifact_id: str | None = None

    @property
    def source_workspace_artifact_ids(self) -> list[str]:
        return [
            self.source_map_artifact_id,
            *self.evidence_artifact_ids,
            *self.source_artifact_ids,
            *self.quote_artifact_ids,
            *([self.workspace_artifact_id] if self.workspace_artifact_id else []),
            self.triage_artifact_id,
            self.triage_gate_artifact_id,
            *([self.corpus_brief_artifact_id] if self.corpus_brief_artifact_id else []),
            *([self.corpus_brief_audit_artifact_id] if self.corpus_brief_audit_artifact_id else []),
            *([self.corpus_brief_shadow_audit_artifact_id] if self.corpus_brief_shadow_audit_artifact_id else []),
        ]


def triage_runtime_values(
    triage_state: TriageState,
) -> tuple[CorpusPreparationResult, EvidenceToolset, list, SourceMap, list[SourceCard], list[QuoteCard], Any]:
    return (
        triage_state.corpus,
        triage_state.evidence_toolset,
        triage_state.evidence_tools,
        triage_state.source_map,
        triage_state.source_cards,
        triage_state.quote_cards,
        triage_state.source_map.source_triage,
    )
