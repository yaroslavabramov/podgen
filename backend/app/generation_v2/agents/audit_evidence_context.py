from __future__ import annotations

from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.agents.editorial_context import trim as _trim
from app.generation_v2.agents.tool_sets import AUDIT_TOOL_NAMES, phase_tools
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import ResearchMemo
from app.generation_v2.tools.evidence_refs import SourceRefContext, source_ref_for_id


def evidence_support_text(evidence: dict[str, Any]) -> str:
    metadata = evidence.get("metadata") or {}
    return _trim(
        model_preview_text(
            metadata.get("support_summary")
            or metadata.get("quote_text")
            or evidence.get("query_text")
            or evidence.get("purpose")
            or ""
        ),
        260,
    )


def evidence_preview_snippet(evidence: dict[str, Any]) -> str:
    metadata = evidence.get("metadata") or {}
    return _trim(
        model_preview_text(
            metadata.get("quote_text") or evidence.get("snippet_text") or metadata.get("support_summary") or ""
        ),
        420,
    )


def audit_evidence_catalog(
    *,
    research_memo: ResearchMemo,
    evidence_records: list[dict],
    context: SourceRefContext,
    source_titles_by_id: dict[str, str],
    workspace: dict[str, Any] | None = None,
    required_preview_limit: int = 4,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], set[str]]:
    evidence_by_id = {str(item.get("id")): item for item in evidence_records if item.get("id")}
    previews_by_atom_ref: dict[str, list[dict[str, Any]]] = {}
    evidence_id_by_ref: dict[str, str] = {}
    required_refs: set[str] = set()
    first_ref_by_evidence_id: dict[str, str] = {}
    next_index = 1
    for atom_idx, atom in enumerate(research_memo.editorial_atoms, start=1):
        atom_ref = f"A{atom_idx:02d}"
        atom_previews: list[dict[str, Any]] = []
        suspicious = atom.source_coverage_status in {"partial", "weak"} or len(atom.source_ids) <= 1 or atom.kind == "open_question"
        for evidence_index, evidence_id in enumerate(atom.evidence_ids):
            evidence = evidence_by_id.get(str(evidence_id))
            if not evidence:
                continue
            evidence_ref = f"E{next_index:03d}"
            next_index += 1
            source_id = str(evidence.get("source_id") or "")
            source_ref = source_ref_for_id(source_id, context)
            priority = "optional"
            within_required_preview = evidence_index < required_preview_limit
            if within_required_preview and atom.story_weight == "core" and not atom_previews:
                priority = "required"
                required_refs.add(evidence_ref)
            elif within_required_preview and suspicious:
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
            first_ref_by_evidence_id.setdefault(str(evidence_id), evidence_ref)
        previews_by_atom_ref[atom_ref] = atom_previews

    # Question outcomes are part of the research handoff even when their
    # evidence was not promoted into a memo atom. Give that evidence the same
    # public E-ref/read surface as atom evidence, while keeping backend ids out
    # of model context. One available evidence item per answered question must
    # be inspected before an audit can pass.
    for job in (workspace or {}).get("investigation_jobs") or []:
        if not isinstance(job, dict):
            continue
        for outcome in job.get("question_outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            available_refs: list[str] = []
            for evidence_id in outcome.get("evidence_ids") or []:
                evidence_id = str(evidence_id or "").strip()
                if not evidence_id or evidence_id not in evidence_by_id:
                    continue
                evidence_ref = first_ref_by_evidence_id.get(evidence_id)
                if evidence_ref is None:
                    evidence_ref = f"E{next_index:03d}"
                    next_index += 1
                    evidence_id_by_ref[evidence_ref] = evidence_id
                    first_ref_by_evidence_id[evidence_id] = evidence_ref
                if evidence_ref not in available_refs:
                    available_refs.append(evidence_ref)
            if str(outcome.get("status") or "") == "answered" and available_refs:
                required_refs.add(available_refs[0])
    return previews_by_atom_ref, evidence_id_by_ref, required_refs


def audit_evidence_previews(
    *,
    research_memo: ResearchMemo,
    evidence_records: list[dict],
    context: SourceRefContext,
    source_titles_by_id: dict[str, str],
    workspace: dict[str, Any] | None = None,
    max_per_atom: int = 4,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], set[str]]:
    catalog, evidence_id_by_ref, required_refs = audit_evidence_catalog(
        research_memo=research_memo,
        evidence_records=evidence_records,
        context=context,
        source_titles_by_id=source_titles_by_id,
        workspace=workspace,
        required_preview_limit=max_per_atom,
    )
    return (
        {atom_ref: previews[:max_per_atom] for atom_ref, previews in catalog.items()},
        evidence_id_by_ref,
        required_refs,
    )


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
    if isinstance(value, str):
        return model_preview_text(value, collapse_whitespace=False)
    return value


def audit_tools_with_evidence_refs(
    evidence_tools: list[AgentTool] | None,
    *,
    evidence_id_by_ref: dict[str, str],
    read_refs: set[str],
    evidence_previews_by_atom_ref: dict[str, list[dict[str, Any]]] | None = None,
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
    if evidence_previews_by_atom_ref is not None:

        async def _list_audit_evidence(args: dict[str, Any]) -> dict[str, Any]:
            atom_ref = str(args.get("atom_ref") or "").strip()
            if atom_ref not in evidence_previews_by_atom_ref:
                return {"accepted": False, "error": "unknown_atom_ref", "atom_ref": atom_ref}
            cursor_text = str(args.get("cursor") or "0")
            cursor = int(cursor_text) if cursor_text.isdigit() else 0
            limit = max(1, min(int(args.get("limit") or 10), 20))
            previews = evidence_previews_by_atom_ref[atom_ref]
            items = previews[cursor : cursor + limit]
            next_offset = cursor + len(items)
            return {
                "accepted": True,
                "atom_ref": atom_ref,
                "items": items,
                "page": {
                    "total_count": len(previews),
                    "returned_count": len(items),
                    "omitted_count": max(0, len(previews) - next_offset),
                    "cursor": str(cursor),
                    "next_cursor": str(next_offset) if next_offset < len(previews) else "",
                },
            }

        tools.append(
            AgentTool(
                name="list_audit_evidence",
                description=(
                    "Page through compact evidence previews for one atom when audit_input_json reports omitted evidence."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "atom_ref": {"type": "string"},
                        "cursor": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["atom_ref", "cursor", "limit"],
                },
                handler=_list_audit_evidence,
            )
        )
    return tools


__all__ = [
    "audit_evidence_catalog",
    "audit_evidence_previews",
    "audit_tools_with_evidence_refs",
    "evidence_preview_snippet",
    "evidence_support_text",
    "strip_audit_backend_ids",
]
