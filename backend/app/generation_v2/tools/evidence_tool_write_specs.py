from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.tools.evidence_write_validation import (
    SOURCE_MAP_WRITE_LIST_LIMITS,
    SOURCE_MAP_WRITE_TEXT_LIMITS,
)


def _text(tool_name: str, field: str) -> dict[str, Any]:
    return {"type": "string", "maxLength": SOURCE_MAP_WRITE_TEXT_LIMITS[tool_name][field]}


def _strings(tool_name: str, field: str) -> dict[str, Any]:
    max_items, max_length = SOURCE_MAP_WRITE_LIST_LIMITS[tool_name][field]
    return {
        "type": "array",
        "maxItems": max_items,
        "items": {"type": "string", "maxLength": max_length},
    }


def evidence_write_tools(owner: Any) -> list[AgentTool]:
    return [
        AgentTool(
            name="select_evidence",
            description=(
                "Write action: explicitly select an observed EvidenceItem as working support for the SourceMap, research, quote, relationship, gap, or citation. "
                "Only selected evidence can enter SourceMap. Provide the evidence_id exactly as returned by file_search/read/search tools."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "evidence_id": _text("select_evidence", "evidence_id"),
                    "purpose": _text("select_evidence", "purpose"),
                    "role": _text("select_evidence", "role"),
                    "rationale": _text("select_evidence", "rationale"),
                },
                "required": ["evidence_id", "purpose", "role", "rationale"],
            },
            handler=owner.select_evidence_tool,
        ),
        AgentTool(
            name="upsert_source_card",
            description=(
                "Triage write action: create/update a SourceCard from evidence returned by read/search tools. "
                "Included evidence_ids are marked as selected for SourceMap lineage."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": _text("upsert_source_card", "source_ref"),
                    "summary": _text("upsert_source_card", "summary"),
                    "useful_for": _strings("upsert_source_card", "useful_for"),
                    "evidence_ids": _strings("upsert_source_card", "evidence_ids"),
                    "gaps_or_cautions": _strings("upsert_source_card", "gaps_or_cautions"),
                },
                "required": ["source_ref", "summary", "useful_for", "evidence_ids", "gaps_or_cautions"],
            },
            handler=owner.upsert_source_card_tool,
        ),
        AgentTool(
            name="create_quote_card",
            description=(
                "Triage write action: create an optional QuoteCard from an evidence_id returned by read/search tools. "
                "Use only for an obvious quote seed; quote text must come from the evidence snippet/window."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "evidence_id": _text("create_quote_card", "evidence_id"),
                    "quote": _text("create_quote_card", "quote"),
                    "locator": _text("create_quote_card", "locator"),
                    "narrative_use": _text("create_quote_card", "narrative_use"),
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "confidence_reason": _text("create_quote_card", "confidence_reason"),
                },
                "required": [
                    "evidence_id",
                    "quote",
                    "locator",
                    "narrative_use",
                    "confidence",
                    "confidence_reason",
                ],
            },
            handler=owner.create_quote_card_tool,
        ),
        AgentTool(
            name="record_source_relationship",
            description=(
                "Triage write action: record duplicate, near_duplicate, primary_to_secondary, contradicts, updates, or background_for relationships. "
                "Use evidence_ids returned by read/search tools; included evidence_ids are marked as selected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": _text("record_source_relationship", "source_ref"),
                    "related_source_ref": _text("record_source_relationship", "related_source_ref"),
                    "relationship": _text("record_source_relationship", "relationship"),
                    "evidence_ids": _strings("record_source_relationship", "evidence_ids"),
                    "rationale": _text("record_source_relationship", "rationale"),
                },
                "required": ["source_ref", "related_source_ref", "relationship", "evidence_ids", "rationale"],
            },
            handler=owner.record_source_relationship_tool,
        ),
        AgentTool(
            name="record_source_cluster",
            description=(
                "Triage write action: group related sources into a compact source cluster by origin, evidentiary role, topic coverage, duplication/mirroring, update relationship, or quality level. "
                "Use this to map corpus structure for later research, not to make final episode claims."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "label": _text("record_source_cluster", "label"),
                    "source_refs": _strings("record_source_cluster", "source_refs"),
                    "primary_source_refs": _strings("record_source_cluster", "primary_source_refs"),
                    "secondary_source_refs": _strings("record_source_cluster", "secondary_source_refs"),
                    "duplicate_source_refs": _strings("record_source_cluster", "duplicate_source_refs"),
                    "evidence_ids": _strings("record_source_cluster", "evidence_ids"),
                    "rationale": _text("record_source_cluster", "rationale"),
                },
                "required": [
                    "label",
                    "source_refs",
                    "primary_source_refs",
                    "secondary_source_refs",
                    "duplicate_source_refs",
                    "evidence_ids",
                    "rationale",
                ],
            },
            handler=owner.record_source_cluster_tool,
        ),
        AgentTool(
            name="record_gap",
            description=(
                "Triage write action: record a gap, contradiction, caution, missing coverage area, or source-quality warning. "
                "Use evidence_ids when a read/search result grounds the gap; included evidence_ids are marked as selected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": _text("record_gap", "kind"),
                    "description": _text("record_gap", "description"),
                    "source_refs": _strings("record_gap", "source_refs"),
                    "evidence_ids": _strings("record_gap", "evidence_ids"),
                },
                "required": ["kind", "description", "source_refs", "evidence_ids"],
            },
            handler=owner.record_gap_tool,
        ),
        AgentTool(
            name="finalize_source_map",
            description=(
                "Write action: signal that triage write actions are complete and SourceMap should be assembled from selected evidence and write actions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": _text("finalize_source_map", "summary"),
                    "ready": {"type": "boolean"},
                },
                "required": ["summary", "ready"],
            },
            handler=owner.finalize_source_map_tool,
        ),
    ]


__all__ = ["evidence_write_tools"]
