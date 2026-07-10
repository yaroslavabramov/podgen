from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool


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
                    "evidence_id": {"type": "string"},
                    "purpose": {"type": "string"},
                    "role": {"type": "string"},
                    "rationale": {"type": "string"},
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
                    "source_ref": {"type": "string"},
                    "summary": {"type": "string"},
                    "useful_for": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "gaps_or_cautions": {"type": "array", "items": {"type": "string"}},
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
                    "evidence_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "locator": {"type": "string"},
                    "narrative_use": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "confidence_reason": {"type": "string"},
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
                    "source_ref": {"type": "string"},
                    "related_source_ref": {"type": "string"},
                    "relationship": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
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
                    "label": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "primary_source_refs": {"type": "array", "items": {"type": "string"}},
                    "secondary_source_refs": {"type": "array", "items": {"type": "string"}},
                    "duplicate_source_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
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
                    "kind": {"type": "string"},
                    "description": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
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
                    "summary": {"type": "string"},
                    "ready": {"type": "boolean"},
                },
                "required": ["summary", "ready"],
            },
            handler=owner.finalize_source_map_tool,
        ),
    ]


__all__ = ["evidence_write_tools"]
