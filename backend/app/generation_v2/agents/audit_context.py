from __future__ import annotations

from typing import Any

from app.generation_v2.agents.editorial_context import (
    source_ref_context as _source_ref_context,
    trim as _trim,
)
from app.generation_v2.agents.audit_evidence_context import (
    audit_evidence_catalog,
    audit_evidence_previews,
    audit_tools_with_evidence_refs,
    evidence_preview_snippet,
    evidence_support_text,
    strip_audit_backend_ids,
)
from app.generation_v2.agents.treatment_context import source_refs_for_ids as _source_refs_for_ids
from app.generation_v2.research_source_map_prompting import source_map_research_page
from app.generation_v2.research_workspace import source_map_research_summary
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import ResearchAudit, ResearchMemo, SourceMap
from app.models.generation_run import GenerationRun


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = str(value or "")
    return {
        f"{name}_preview": _trim(text, limit),
        f"{name}_chars": len(text),
        f"{name}_truncated": len(text) > limit,
    }


def _preview_items(values: list[Any], limit: int) -> list[dict[str, Any]]:
    return [_preview_fields("text", item, limit) for item in values]


def _page_metadata(*, total: int, cursor: int, returned: int) -> dict[str, Any]:
    next_offset = cursor + returned
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - next_offset),
        "cursor": str(cursor),
        "next_cursor": str(next_offset) if next_offset < total else "",
    }


def _processed_job_preview(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": item.get("job_id"),
        "status": item.get("status"),
        **_preview_fields("summary", item.get("summary") or "", 500),
    }


def _gap_preview(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "gap_id": item.get("gap_id"),
        **_preview_fields("question", item.get("question") or "", 450),
        "category": item.get("category"),
        "severity": item.get("severity"),
        "treatment_relevance": item.get("treatment_relevance") or "supporting",
        "status": item.get("status"),
        "source_refs": item.get("source_refs") or [],
        "atom_refs": item.get("atom_refs") or [],
        "evidence_count": len(item.get("evidence_ids") or []),
        "has_evidence": bool(item.get("evidence_ids") or []),
        **_preview_fields("resolution_note", item.get("resolution_note") or "", 350),
    }


def _risk_preview(item: Any) -> dict[str, Any]:
    return {
        **_preview_fields("risk", item.get("risk") if isinstance(item, dict) else item, 450),
        "severity": str(item.get("severity") or "medium") if isinstance(item, dict) else "medium",
        "source_refs": (item.get("source_refs") or []) if isinstance(item, dict) else [],
        "atom_refs": (item.get("atom_refs") or []) if isinstance(item, dict) else [],
        "evidence_count": len(item.get("evidence_ids") or []) if isinstance(item, dict) else 0,
        "has_evidence": bool(item.get("evidence_ids") or []) if isinstance(item, dict) else False,
    }


def _atom_preview(
    atom: Any,
    *,
    atom_ref: str,
    context: Any,
    evidence_previews: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "atom_ref": atom_ref,
        "kind": atom.kind,
        "title": atom.title,
        **_preview_fields("claim", atom.claim or atom.explanation, 700),
        **_preview_fields("explanation", atom.explanation, 900),
        **_preview_fields("story_value", atom.story_value, 600),
        "source_refs": _source_refs_for_ids(atom.source_ids, context),
        "quote_refs": atom.quote_refs,
        "evidence_previews": evidence_previews[:4],
        "evidence_count": len(atom.evidence_ids),
        "evidence_preview_count": min(4, len(evidence_previews)),
        "evidence_preview_omitted_count": max(0, len(atom.evidence_ids) - min(4, len(evidence_previews))),
        "confidence": atom.confidence,
        **_preview_fields("risk", atom.risk, 450),
        "source_coverage_status": atom.source_coverage_status,
        "story_weight": atom.story_weight,
        **_preview_fields("treatment_role_hint", atom.treatment_role_hint, 400),
    }


def _full_atom_for_audit(
    atom: Any,
    *,
    atom_ref: str,
    context: Any,
    evidence_previews: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "atom_ref": atom_ref,
        "kind": atom.kind,
        "title": atom.title,
        "claim": atom.claim or atom.explanation,
        "explanation": atom.explanation,
        "story_value": atom.story_value,
        "source_refs": _source_refs_for_ids(atom.source_ids, context),
        "quote_refs": atom.quote_refs,
        "evidence_count": len(atom.evidence_ids),
        "evidence_preview_count": 0,
        "evidence_preview_omitted_count": len(evidence_previews),
        "confidence": atom.confidence,
        "risk": atom.risk,
        "source_coverage_status": atom.source_coverage_status,
        "story_weight": atom.story_weight,
        "treatment_role_hint": atom.treatment_role_hint,
    }


def _audit_page_item(
    section: str,
    item: Any,
    index: int,
    *,
    context: Any,
    evidence_previews_by_atom_ref: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if section == "memo_supported_answer":
        return {"supported_answer": str(item)}
    if section == "memo_atoms":
        atom_ref = f"A{index + 1:02d}"
        return _full_atom_for_audit(
            item,
            atom_ref=atom_ref,
            context=context,
            evidence_previews=evidence_previews_by_atom_ref.get(atom_ref, []),
        )
    if section in {"memo_source_coverage", "memo_open_questions", "memo_risks"}:
        return {"item_number": index + 1, "text": str(item)}
    if section == "processed_jobs":
        return {
            "job_id": item.get("job_id"),
            "status": item.get("status"),
            "summary": str(item.get("summary") or ""),
        }
    if section == "research_gaps":
        return {
            "gap_id": item.get("gap_id"),
            "question": str(item.get("question") or ""),
            "category": item.get("category"),
            "severity": item.get("severity"),
            "treatment_relevance": item.get("treatment_relevance") or "supporting",
            "status": item.get("status"),
            "source_refs": item.get("source_refs") or [],
            "atom_refs": item.get("atom_refs") or [],
            "evidence_count": len(item.get("evidence_ids") or []),
            "resolution_note": str(item.get("resolution_note") or ""),
        }
    if section == "workspace_risks":
        return {
            "risk": str(item.get("risk") if isinstance(item, dict) else item),
            "severity": str(item.get("severity") or "medium") if isinstance(item, dict) else "medium",
            "source_refs": (item.get("source_refs") or []) if isinstance(item, dict) else [],
            "atom_refs": (item.get("atom_refs") or []) if isinstance(item, dict) else [],
            "evidence_count": len(item.get("evidence_ids") or []) if isinstance(item, dict) else 0,
        }
    raise ValueError(f"Unknown audit page section: {section}")


def audit_workspace_page_tool(
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap,
    workspace: dict[str, Any] | None,
    evidence_previews_by_atom_ref: dict[str, list[dict[str, Any]]],
) -> AgentTool:
    workspace = workspace or {}
    context = _source_ref_context(source_inventory.source_cards)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        section = str(args.get("section") or "")
        cursor_text = str(args.get("cursor") or "0")
        cursor = int(cursor_text) if cursor_text.isdigit() else 0
        cursor = max(0, cursor)
        requested_limit = max(1, int(args.get("limit") or 3))
        if section.startswith("source_map_"):
            try:
                page = source_map_research_page(
                    source_map,
                    source_inventory,
                    section=section,
                    cursor=cursor,
                    limit=min(requested_limit, 3),
                    include_evidence_ids=False,
                )
            except ValueError as exc:
                return {"accepted": False, "error": str(exc)}
            return {"accepted": True, **page}

        memo_atoms = list(research_memo.editorial_atoms)
        processed_jobs = [item for item in workspace.get("processed_jobs") or [] if isinstance(item, dict)]
        research_gaps = [item for item in workspace.get("research_gaps") or [] if isinstance(item, dict)]
        workspace_risks = list(workspace.get("risk_register") or [])
        values: list[Any]
        max_limit = 5
        if section == "memo_supported_answer":
            values = [research_memo.thesis]
            max_limit = 1
        elif section == "memo_atoms":
            values = memo_atoms
            max_limit = 1
        elif section == "memo_source_coverage":
            values = list(research_memo.source_coverage)
        elif section == "memo_open_questions":
            values = list(research_memo.open_questions)
        elif section == "memo_risks":
            values = list(research_memo.risk_register)
        elif section == "processed_jobs":
            values = processed_jobs
        elif section == "research_gaps":
            values = research_gaps
            max_limit = 3
        elif section == "workspace_risks":
            values = workspace_risks
            max_limit = 3
        else:
            return {"accepted": False, "error": "unknown_research_workspace_section", "section": section}

        limit = min(requested_limit, max_limit)
        page_values = values[cursor : cursor + limit]
        items = [
            _audit_page_item(
                section,
                item,
                cursor + offset,
                context=context,
                evidence_previews_by_atom_ref=evidence_previews_by_atom_ref,
            )
            for offset, item in enumerate(page_values)
        ]
        return {
            "accepted": True,
            "section": section,
            "items": items,
            "page": _page_metadata(total=len(values), cursor=cursor, returned=len(items)),
        }

    return AgentTool(
        name="list_research_workspace_items",
        description=(
            "Page through full validated memo/workspace and SourceMap items when audit_input_json "
            "reports omitted or truncated context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": [
                        "memo_supported_answer",
                        "memo_atoms",
                        "memo_source_coverage",
                        "memo_open_questions",
                        "memo_risks",
                        "processed_jobs",
                        "research_gaps",
                        "workspace_risks",
                        "source_map_cards",
                        "source_map_quotes",
                        "source_map_clusters",
                        "source_map_relationships",
                        "source_map_gaps",
                    ],
                },
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["section", "cursor", "limit"],
        },
        handler=handler,
    )


def research_audit_input(
    *,
    run: GenerationRun,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap | None,
    workspace: dict[str, Any] | None,
    evidence_records: list[dict] | None = None,
) -> dict[str, Any]:
    inventory = source_inventory or source_map
    context = _source_ref_context(inventory.source_cards)
    workspace = workspace or {}
    source_titles_by_id = {card.source_id: card.title for card in inventory.source_cards}
    evidence_previews_by_atom_ref, _, _ = audit_evidence_previews(
        research_memo=research_memo,
        evidence_records=evidence_records or [],
        context=context,
        source_titles_by_id=source_titles_by_id,
    )
    atom_drafts = [item for item in workspace.get("atom_drafts") or [] if isinstance(item, dict)]
    active_drafts = [item for item in atom_drafts if not item.get("merged_into")]
    atom_limit = 12
    list_limit = 20
    memo_atoms = list(research_memo.editorial_atoms)
    processed_jobs = [item for item in workspace.get("processed_jobs") or [] if isinstance(item, dict)]
    research_gaps = [item for item in workspace.get("research_gaps") or [] if isinstance(item, dict)]
    workspace_risks = list(workspace.get("risk_register") or [])
    return {
        "topic": str((run.config or {}).get("user_prompt") or ""),
        "language": str((run.config or {}).get("language") or "en"),
        "duration_minutes": int((run.config or {}).get("duration_target", 1800)) // 60,
        "research_memo": {
            "title": research_memo.title,
            **_preview_fields("supported_answer", research_memo.thesis, 500),
            "editorial_atoms": [
                _atom_preview(
                    atom,
                    atom_ref=f"A{idx:02d}",
                    context=context,
                    evidence_previews=evidence_previews_by_atom_ref.get(f"A{idx:02d}", []),
                )
                for idx, atom in enumerate(memo_atoms[:atom_limit], start=1)
            ],
            "editorial_atom_count": len(memo_atoms),
            "source_coverage_previews": _preview_items(research_memo.source_coverage[:list_limit], 450),
            "source_coverage_count": len(research_memo.source_coverage),
            "open_question_previews": _preview_items(research_memo.open_questions[:list_limit], 450),
            "open_question_count": len(research_memo.open_questions),
            "risk_previews": _preview_items(research_memo.risk_register[:list_limit], 450),
            "risk_count": len(research_memo.risk_register),
            "pages": {
                "memo_supported_answer": _page_metadata(
                    total=1,
                    cursor=0,
                    returned=1 if len(research_memo.thesis) <= 500 else 0,
                ),
                "memo_atoms": _page_metadata(total=len(memo_atoms), cursor=0, returned=min(len(memo_atoms), atom_limit)),
                "memo_source_coverage": _page_metadata(
                    total=len(research_memo.source_coverage),
                    cursor=0,
                    returned=min(len(research_memo.source_coverage), list_limit),
                ),
                "memo_open_questions": _page_metadata(
                    total=len(research_memo.open_questions),
                    cursor=0,
                    returned=min(len(research_memo.open_questions), list_limit),
                ),
                "memo_risks": _page_metadata(
                    total=len(research_memo.risk_register),
                    cursor=0,
                    returned=min(len(research_memo.risk_register), list_limit),
                ),
            },
        },
        "workspace": {
            "status": workspace.get("status"),
            "job_count": len(workspace.get("investigation_jobs") or []),
            "processed_jobs": [_processed_job_preview(item) for item in processed_jobs[:list_limit]],
            "processed_job_count": len(processed_jobs),
            "covered_source_refs": workspace.get("covered_source_refs") or [],
            "selected_evidence_count": len(workspace.get("selected_evidence_ids") or []),
            "active_atom_draft_count": len(active_drafts),
            "merged_atom_draft_count": len(atom_drafts) - len(active_drafts),
            "research_gaps": [
                _gap_preview(item)
                for item in research_gaps[:list_limit]
            ],
            "research_gap_count": len(research_gaps),
            "risk_register": [
                _risk_preview(item)
                for item in workspace_risks[:list_limit]
            ],
            "risk_count": len(workspace_risks),
            "pages": {
                "processed_jobs": _page_metadata(
                    total=len(processed_jobs), cursor=0, returned=min(len(processed_jobs), list_limit)
                ),
                "research_gaps": _page_metadata(
                    total=len(research_gaps), cursor=0, returned=min(len(research_gaps), list_limit)
                ),
                "workspace_risks": _page_metadata(
                    total=len(workspace_risks), cursor=0, returned=min(len(workspace_risks), list_limit)
                ),
            },
        },
        "source_map_summary": source_map_research_summary(source_map, inventory, include_evidence_ids=False),
    }


def normalize_research_audit(audit: ResearchAudit) -> ResearchAudit:
    repair_issues = [issue for issue in audit.issues if issue.severity in {"blocking", "major"}]
    issue_targets = {
        issue.target_step
        for issue in repair_issues
        if issue.target_step != "none"
    }
    declared_targets = {
        target
        for target in audit.repair_targets
        if target != "none"
    }
    targets = sorted(
        {
            *issue_targets,
            *declared_targets,
        }
    )
    if targets:
        audit.repair_targets = targets
    else:
        audit.repair_targets = []
    audit.blocking = bool(
        repair_issues
        or audit.blocking
        or targets
        or audit.missing_material
        or audit.required_followups
    )
    audit.passed = not audit.blocking
    if audit.passed:
        audit.missing_material = []
        audit.required_followups = []
    return audit

__all__ = [
    "audit_evidence_catalog",
    "audit_evidence_previews",
    "audit_tools_with_evidence_refs",
    "audit_workspace_page_tool",
    "evidence_preview_snippet",
    "evidence_support_text",
    "normalize_research_audit",
    "research_audit_input",
    "strip_audit_backend_ids",
]
