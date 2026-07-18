from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTerminalResult, AgentTool
from app.generation_v2.schemas import SourceTriageCompletion
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
    def source_map_terminal_completion(
        arguments: dict[str, Any],
        output: dict[str, Any],
    ) -> SourceTriageCompletion:
        payload = output.get("terminal_completion")
        if isinstance(payload, dict):
            return SourceTriageCompletion.model_validate(payload)
        # Compatibility for the legacy non-workspace finalizer path.
        return SourceTriageCompletion(
            ready=True,
            summary=str(arguments.get("summary") or "Source map finalized."),
            notes=[],
        )

    async def source_map_resume_loader() -> AgentTerminalResult | None:
        from app.generation_v2.source_triage_workspace_persistence import load_source_triage_workspace
        from app.generation_v2.source_triage_lineage import coverage_terminal_completion_payload

        workspace = await load_source_triage_workspace(owner.db, owner.run.id)
        finalization = (workspace or {}).get("source_map_finalization") or {}
        if finalization.get("ready") is not True:
            return None
        return AgentTerminalResult(
            accepted=True,
            completion=SourceTriageCompletion.model_validate(
                coverage_terminal_completion_payload(finalization)
            ),
        )

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
                "additionalProperties": False,
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
                "additionalProperties": False,
            },
            handler=owner.upsert_source_card_tool,
        ),
        AgentTool(
            name="create_quote_card",
            description=(
                "Triage write action: create an optional QuoteCard from an evidence_id returned by read/search tools. "
                "Use only for an obvious quote seed; quote text must come from the evidence snippet/window. "
                "The locator is copied from the canonical EvidenceItem."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "evidence_id": _text("create_quote_card", "evidence_id"),
                    "quote": _text("create_quote_card", "quote"),
                    "narrative_use": _text("create_quote_card", "narrative_use"),
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "confidence_reason": _text("create_quote_card", "confidence_reason"),
                },
                "required": [
                    "evidence_id",
                    "quote",
                    "narrative_use",
                    "confidence",
                    "confidence_reason",
                ],
                "additionalProperties": False,
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
                    "relationship": {
                        "type": "string",
                        "enum": [
                            "primary_to_secondary",
                            "duplicate",
                            "near_duplicate",
                            "contradicts",
                            "updates",
                            "background_for",
                        ],
                        "description": (
                            "Direction is source_ref -> related_source_ref: primary_to_secondary means source_ref "
                            "is the primary/original; updates means source_ref updates related_source_ref; "
                            "background_for means source_ref provides background for related_source_ref."
                        ),
                    },
                    "evidence_ids": _strings("record_source_relationship", "evidence_ids"),
                    "rationale": _text("record_source_relationship", "rationale"),
                },
                "required": ["source_ref", "related_source_ref", "relationship", "evidence_ids", "rationale"],
                "additionalProperties": False,
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
                "additionalProperties": False,
            },
            handler=owner.record_source_cluster_tool,
        ),
        AgentTool(
            name="record_gap",
            description=(
                "Triage write action: record a gap, contradiction, caution, missing coverage area, or source-quality warning. "
                "Use evidence_ids when a read/search result grounds the gap; every evidence_id must belong to one "
                "of the declared source_refs. Attach relevant source_refs and/or persisted question_ids as durable "
                "lineage. Included evidence_ids are marked as selected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": _text("record_gap", "kind"),
                    "description": _text("record_gap", "description"),
                    "source_refs": _strings("record_gap", "source_refs"),
                    "question_ids": _strings("record_gap", "question_ids"),
                    "evidence_ids": _strings("record_gap", "evidence_ids"),
                },
                "required": ["kind", "description", "source_refs", "question_ids", "evidence_ids"],
                "additionalProperties": False,
            },
            handler=owner.record_gap_tool,
        ),
        AgentTool(
            name="list_source_map_gaps",
            description=(
                "List current unresolved SourceMap gaps as compact gap_refs. Page until next_cursor is empty "
                "before deciding that an older gap is absent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cursor": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                },
                "required": ["cursor", "limit"],
                "additionalProperties": False,
            },
            handler=owner.list_source_map_gaps_tool,
        ),
        AgentTool(
            name="resolve_gap",
            description=(
                "Close one current SourceMap gap only when the repaired durable evidence/state makes it obsolete. "
                "Use a stable gap_ref returned by list_source_map_gaps and cite a current assessment, outcome, or "
                "synthesis that carries the supporting saved evidence; history is preserved."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "gap_ref": _text("resolve_gap", "gap_ref"),
                    "basis_ref": _text("resolve_gap", "basis_ref"),
                    "reason": _text("resolve_gap", "reason"),
                },
                "required": ["gap_ref", "basis_ref", "reason"],
                "additionalProperties": False,
            },
            handler=owner.resolve_gap_tool,
        ),
        AgentTool(
            name="resolve_source_map_repair",
            description=(
                "Explicitly discard one backend-identified invalid SourceMap relationship, cluster, or gap record. "
                "Use the stable repair_ref from source_map_repairs; record a valid replacement separately only "
                "when existing persisted evidence supports it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repair_ref": _text("resolve_source_map_repair", "repair_ref"),
                    "reason": _text("resolve_source_map_repair", "reason"),
                },
                "required": ["repair_ref", "reason"],
                "additionalProperties": False,
            },
            handler=owner.resolve_source_map_repair_tool,
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
                "additionalProperties": False,
            },
            handler=owner.finalize_source_map_tool,
            terminal=True,
            terminal_result_builder=source_map_terminal_completion,
            terminal_resume_loader=source_map_resume_loader,
        ),
    ]


__all__ = ["evidence_write_tools"]
