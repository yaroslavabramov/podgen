from __future__ import annotations

from app.models.generation_v2 import SourceRepresentationKind


_CHUNK_BACKED_VERSION_KINDS = (
    SourceRepresentationKind.EXTRACTED_TEXT,
    SourceRepresentationKind.TRANSCRIPT,
)
_TRIAGE_READING_TOOL_NAMES = {
    "list_sources",
    "list_source_versions",
    "search_sources",
    "search_evidence",
    "find_quotes",
    "read_evidence",
    "read_source",
    "read_source_window",
    "read_source_with_model",
    "skim_source",
    "inspect_source",
    "search_within_source",
    "compare_sources",
    "select_evidence",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_gap",
    "finalize_source_map",
    "openai.file_search.observed",
}
_WRITE_TOOL_NAMES = {
    "select_evidence",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_gap",
    "finalize_source_map",
}
EVIDENCE_STATUS_OBSERVED = "observed"
EVIDENCE_STATUS_SELECTED = "selected"
EVIDENCE_STATUS_CITED = "cited"
EVIDENCE_STATUS_REJECTED = "rejected"

__all__ = [
    "_CHUNK_BACKED_VERSION_KINDS",
    "_TRIAGE_READING_TOOL_NAMES",
    "_WRITE_TOOL_NAMES",
    "EVIDENCE_STATUS_OBSERVED",
    "EVIDENCE_STATUS_SELECTED",
    "EVIDENCE_STATUS_CITED",
    "EVIDENCE_STATUS_REJECTED",
]
