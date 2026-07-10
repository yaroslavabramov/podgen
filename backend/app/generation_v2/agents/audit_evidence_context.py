from __future__ import annotations

from typing import Any

from app.generation_v2.agents.editorial_context import trim as _trim
from app.generation_v2.agents.tool_sets import AUDIT_TOOL_NAMES, phase_tools
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import ResearchMemo
from app.generation_v2.tools.evidence_refs import SourceRefContext, source_ref_for_id


def evidence_support_text(evidence: dict[str, Any]) -> str:
    metadata = evidence.get("metadata") or {}
    return _trim(
        str(metadata.get("support_summary") or metadata.get("quote_text") or evidence.get("query_text") or evidence.get("purpose") or ""),
        260,
    )


def evidence_preview_snippet(evidence: dict[str, Any]) -> str:
    metadata = evidence.get("metadata") or {}
    return _trim(
        str(metadata.get("quote_text") or evidence.get("snippet_text") or metadata.get("support_summary") or ""),
        420,
    )


def audit_evidence_previews(
    *,
    research_memo: ResearchMemo,
    evidence_records: list[dict],
    context: SourceRefContext,
    source_titles_by_id: dict[str, str],
    max_per_atom: int = 4,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], set[str]]:
    evidence_by_id = {str(item.get("id")): item for item in evidence_records if item.get("id")}
    previews_by_atom_ref: dict[str, list[dict[str, Any]]] = {}
    evidence_id_by_ref: dict[str, str] = {}
    required_refs: set[str] = set()
    next_index = 1
    for atom_idx, atom in enumerate(research_memo.editorial_atoms, start=1):
        atom_ref = f"A{atom_idx:02d}"
        atom_previews: list[dict[str, Any]] = []
        suspicious = atom.source_coverage_status in {"partial", "weak"} or len(atom.source_ids) <= 1 or atom.kind == "open_question"
        for evidence_id in atom.evidence_ids[:max_per_atom]:
            evidence = evidence_by_id.get(str(evidence_id))
            if not evidence:
                continue
            evidence_ref = f"E{next_index:03d}"
            next_index += 1
            source_id = str(evidence.get("source_id") or "")
            source_ref = source_ref_for_id(source_id, context)
            priority = "optional"
            if atom.story_weight == "core" and not atom_previews:
                priority = "required"
                required_refs.add(evidence_ref)
            elif suspicious:
                priority = "required"
                required_refs.add(evidence_ref)
            preview = {
                "evidence_ref": evidence_ref,
                "atom_ref": atom_ref,
                "source_ref": source_ref,
                "source_title": _trim(source_titles_by_id.get(source_id, ""), 180),
                "locator": _trim(str(evidence.get("locator_text") or evidence.get("locator_type") or ""), 160),
                "short_snippet": evidence_preview_snippet(evidence),
                "supports_claim": evidence_support_text(evidence),
                "read_priority": priority,
            }
            atom_previews.append(preview)
            evidence_id_by_ref[evidence_ref] = str(evidence_id)
        previews_by_atom_ref[atom_ref] = atom_previews
    return previews_by_atom_ref, evidence_id_by_ref, required_refs


def strip_audit_backend_ids(value: Any) -> Any:
    blocked = {
        "evidence_id",
        "evidence_ids",
        "source_id",
        "source_version_id",
        "provider_file_id",
        "provider_vector_store_id",
        "provider_vector_store_file_id",
        "retrieval_channel",
        "representation_kind",
        "usage_status",
    }
    if isinstance(value, dict):
        return {key: strip_audit_backend_ids(item) for key, item in value.items() if key not in blocked}
    if isinstance(value, list):
        return [strip_audit_backend_ids(item) for item in value]
    return value


def audit_tools_with_evidence_refs(
    evidence_tools: list[AgentTool] | None,
    *,
    evidence_id_by_ref: dict[str, str],
    read_refs: set[str],
) -> list[AgentTool]:
    tools: list[AgentTool] = []
    for tool in phase_tools(evidence_tools, AUDIT_TOOL_NAMES):
        if tool.name != "read_evidence":
            tools.append(tool)
            continue

        async def _read_evidence_by_ref(args: dict[str, Any], *, _tool: AgentTool = tool) -> dict[str, Any]:
            evidence_ref = str(args.get("evidence_ref") or "").strip()
            if not evidence_ref:
                return {"found": False, "error": "missing_evidence_ref"}
            evidence_id = evidence_id_by_ref.get(evidence_ref)
            if not evidence_id:
                return {"found": False, "error": "unknown_evidence_ref", "evidence_ref": evidence_ref}
            args = {key: value for key, value in args.items() if key != "evidence_ref"}
            args["evidence_id"] = evidence_id
            result = await _tool.handler(args)
            found = bool(result.get("found"))
            evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else None
            has_error = bool(result.get("error") or (evidence or {}).get("error"))
            if found and not has_error:
                read_refs.add(evidence_ref)
            sanitized = strip_audit_backend_ids(result)
            sanitized["evidence_ref"] = evidence_ref
            return sanitized

        tools.append(
            AgentTool(
                name="read_evidence",
                description=(
                    "Read one audit evidence preview by evidence_ref from audit_input_json. "
                    "Use this for required or suspicious evidence previews before passing audit."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "evidence_ref": {"type": "string"},
                    },
                    "required": ["evidence_ref"],
                },
                handler=_read_evidence_by_ref,
            )
        )
    return tools


__all__ = [
    "audit_evidence_previews",
    "audit_tools_with_evidence_refs",
    "evidence_preview_snippet",
    "evidence_support_text",
    "strip_audit_backend_ids",
]
