from __future__ import annotations

from typing import Any

from app.generation_v2.model_context_text import model_preview_text

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
from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.research_source_map_prompting import source_map_research_page
from app.generation_v2.research_workspace import source_map_research_summary
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import ResearchAudit, ResearchMemo, SourceMap
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref_for_record
from app.models.generation_run import GenerationRun


def _preview_fields(
    name: str,
    value: Any,
    limit: int,
    *,
    replacements: dict[str, str] | None = None,
) -> dict[str, Any]:
    text = model_preview_text(value, replacements=replacements, collapse_whitespace=False)
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


def _outcome_evidence_refs(
    outcome: dict[str, Any],
    evidence_ref_by_id: dict[str, str],
) -> list[str]:
    refs: list[str] = []
    for evidence_id in outcome.get("evidence_ids") or []:
        evidence_ref = evidence_ref_by_id.get(str(evidence_id or ""))
        if evidence_ref and evidence_ref not in refs:
            refs.append(evidence_ref)
    return refs


def _investigation_job_preview(
    item: dict[str, Any],
    *,
    evidence_ref_by_id: dict[str, str],
    required_evidence_refs: set[str],
) -> dict[str, Any]:
    outcomes = [value for value in item.get("question_outcomes") or [] if isinstance(value, dict)]
    outcome_refs = {
        evidence_ref
        for outcome in outcomes
        for evidence_ref in _outcome_evidence_refs(outcome, evidence_ref_by_id)
    }
    return {
        "job_id": item.get("job_id"),
        "corpus_handoff_id": item.get("corpus_handoff_id"),
        "coverage_question_ids": item.get("coverage_question_ids") or [],
        "status": item.get("status"),
        "question_count": len(item.get("questions") or []),
        "question_outcome_count": len(outcomes),
        "answered_question_count": sum(1 for outcome in outcomes if outcome.get("status") == "answered"),
        "deferred_question_count": sum(1 for outcome in outcomes if outcome.get("status") == "deferred"),
        "outcome_evidence_ref_count": len(outcome_refs),
        "required_outcome_evidence_ref_count": len(outcome_refs & required_evidence_refs),
        **_preview_fields("summary", item.get("summary") or "", 500),
        **_preview_fields("deferred_reason", item.get("deferred_reason") or "", 400),
    }


def _corpus_brief_preview_for_audit(brief: dict[str, Any], context: Any, *, limit: int) -> dict[str, Any]:
    questions = [item for item in brief.get("coverage_questions") or [] if isinstance(item, dict)]
    findings = [item for item in brief.get("source_findings") or [] if isinstance(item, dict)]
    outcomes = [item for item in brief.get("coverage_outcomes") or [] if isinstance(item, dict)]
    handoff = [item for item in brief.get("research_handoff") or [] if isinstance(item, dict)]
    gap_records = [item for item in brief.get("gap_records") or [] if isinstance(item, dict)]
    return {
        **_preview_fields("summary", brief.get("summary") or "", 600),
        "coverage_questions": [
            {
                "question_id": item.get("question_id"),
                "kind": item.get("kind"),
                "importance": item.get("importance"),
                **_preview_fields("question", item.get("question") or "", 600),
                **_preview_fields("rationale", item.get("rationale") or "", 500),
            }
            for item in questions[:limit]
        ],
        "coverage_outcomes": [
            {
                "question_id": item.get("question_id"),
                "status": item.get("status"),
                "source_refs": _source_refs_for_ids(item.get("source_ids") or [], context),
                "evidence_count": len(item.get("evidence_ids") or []),
                **_preview_fields("orientation_summary", item.get("orientation_summary") or "", 600),
                **_preview_fields("missing_detail", item.get("missing_detail") or "", 400),
                **_preview_fields("conflict_summary", item.get("conflict_summary") or "", 400),
                **_preview_fields("deferred_reason", item.get("deferred_reason") or "", 400),
            }
            for item in outcomes[:limit]
        ],
        "source_findings": [
            {
                "finding_id": item.get("finding_id"),
                "source_refs": _source_refs_for_ids([item.get("source_id")], context),
                "question_links": item.get("question_links") or [],
                "evidence_count": len(item.get("evidence_ids") or []),
                **_preview_fields("statement", item.get("statement") or "", 600),
            }
            for item in findings[:limit]
        ],
        "research_handoff": [
            {
                "handoff_id": item.get("handoff_id"),
                "question_ids": item.get("question_ids") or [],
                "action": item.get("action"),
                "priority": item.get("priority"),
                "preferred_source_refs": _source_refs_for_ids(
                    item.get("preferred_source_ids") or [], context
                ),
                "starting_evidence_count": len(item.get("starting_evidence_ids") or []),
                **_preview_fields("objective", item.get("objective") or "", 600),
                **_preview_fields("reason", item.get("reason") or "", 500),
            }
            for item in handoff[:limit]
        ],
        "gap_records": [
            {
                "gap_ref": public_gap_ref_for_record(item),
                "kind": item.get("kind"),
                "source_refs": _source_refs_for_ids(item.get("source_ids") or [], context),
                "evidence_count": len(item.get("evidence_ids") or []),
                **_preview_fields(
                    "description",
                    item.get("description") or "",
                    600,
                    replacements=context.ref_by_source_id,
                ),
            }
            for item in gap_records[:limit]
        ],
        "pages": {
            "corpus_brief_questions": _page_metadata(
                total=len(questions), cursor=0, returned=min(len(questions), limit)
            ),
            "corpus_brief_outcomes": _page_metadata(
                total=len(outcomes), cursor=0, returned=min(len(outcomes), limit)
            ),
            "corpus_brief_findings": _page_metadata(
                total=len(findings), cursor=0, returned=min(len(findings), limit)
            ),
            "corpus_brief_handoff": _page_metadata(
                total=len(handoff), cursor=0, returned=min(len(handoff), limit)
            ),
            "corpus_brief_gap_records": _page_metadata(
                total=len(gap_records), cursor=0, returned=min(len(gap_records), limit)
            ),
        },
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


def _audit_job_page_item(
    section: str,
    item: Any,
    index: int,
    *,
    evidence_ref_by_id: dict[str, str],
    required_evidence_refs: set[str],
) -> dict[str, Any]:
    if section == "jobs":
        return {
            "job_id": item.get("job_id"),
            "corpus_handoff_id": item.get("corpus_handoff_id"),
            "coverage_question_ids": list(item.get("coverage_question_ids") or []),
            "label": str(item.get("label") or ""),
            "purpose": str(item.get("purpose") or ""),
            "status": item.get("status"),
            "summary": str(item.get("summary") or ""),
            "deferred_reason": str(item.get("deferred_reason") or ""),
            "question_count": len(item.get("questions") or []),
            "question_outcome_count": len(item.get("question_outcomes") or []),
        }
    if section == "job_questions":
        job_id, question = item
        return {"job_id": job_id, "question_number": index + 1, "question": str(question)}
    if section == "job_question_outcomes":
        job_id, outcome = item
        evidence_refs = _outcome_evidence_refs(outcome, evidence_ref_by_id)
        return {
            "job_id": job_id,
            "outcome_number": index + 1,
            "question": str(outcome.get("question") or ""),
            "status": outcome.get("status"),
            "note": str(outcome.get("note") or ""),
            "evidence_count": len(outcome.get("evidence_ids") or []),
            "evidence_refs": evidence_refs,
            "required_evidence_refs": [
                evidence_ref for evidence_ref in evidence_refs if evidence_ref in required_evidence_refs
            ],
            "unavailable_evidence_count": max(
                0,
                len(outcome.get("evidence_ids") or []) - len(evidence_refs),
            ),
        }
    raise ValueError(f"Unknown audit job page section: {section}")


def _audit_corpus_brief_page_item(
    section: str,
    item: dict[str, Any],
    context: Any,
    *,
    index: int,
) -> dict[str, Any]:
    if section == "corpus_brief_questions":
        return {
            "question_id": item.get("question_id"),
            "kind": item.get("kind"),
            "importance": item.get("importance"),
            "question": str(item.get("question") or ""),
            "rationale": str(item.get("rationale") or ""),
            "parent_question_id": item.get("parent_question_id"),
        }
    if section == "corpus_brief_outcomes":
        return {
            "question_id": item.get("question_id"),
            "status": item.get("status"),
            "source_refs": _source_refs_for_ids(item.get("source_ids") or [], context),
            "evidence_count": len(item.get("evidence_ids") or []),
            "orientation_summary": str(item.get("orientation_summary") or ""),
            "missing_detail": str(item.get("missing_detail") or ""),
            "conflict_summary": str(item.get("conflict_summary") or ""),
            "deferred_reason": str(item.get("deferred_reason") or ""),
            "next_action": item.get("next_action"),
        }
    if section == "corpus_brief_findings":
        return {
            "finding_id": item.get("finding_id"),
            "source_refs": _source_refs_for_ids([item.get("source_id")], context),
            "statement": str(item.get("statement") or ""),
            "question_links": list(item.get("question_links") or []),
            "scope": dict(item.get("scope") or {}),
            "limitations": list(item.get("limitations") or []),
            "evidence_count": len(item.get("evidence_ids") or []),
        }
    if section == "corpus_brief_handoff":
        return {
            "handoff_id": item.get("handoff_id"),
            "question_ids": list(item.get("question_ids") or []),
            "action": item.get("action"),
            "priority": item.get("priority"),
            "preferred_source_refs": _source_refs_for_ids(
                item.get("preferred_source_ids") or [], context
            ),
            "starting_evidence_count": len(item.get("starting_evidence_ids") or []),
            "objective": str(item.get("objective") or ""),
            "reason": str(item.get("reason") or ""),
        }
    if section == "corpus_brief_gap_records":
        return {
            "gap_ref": public_gap_ref_for_record(item),
            "kind": item.get("kind"),
            "source_refs": _source_refs_for_ids(item.get("source_ids") or [], context),
            "evidence_count": len(item.get("evidence_ids") or []),
            "description": model_preview_text(
                item.get("description") or "",
                replacements=context.ref_by_source_id,
                collapse_whitespace=False,
            ),
        }
    raise ValueError(f"Unknown audit CorpusBrief page section: {section}")


def audit_workspace_page_tool(
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap,
    workspace: dict[str, Any] | None,
    evidence_previews_by_atom_ref: dict[str, list[dict[str, Any]]],
    evidence_ref_by_id: dict[str, str] | None = None,
    required_evidence_refs: set[str] | None = None,
) -> AgentTool:
    workspace = workspace or {}
    evidence_ref_by_id = evidence_ref_by_id or {}
    required_evidence_refs = required_evidence_refs or set()

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        section = str(args.get("section") or "")
        cursor_text = str(args.get("cursor") or "0")
        cursor = int(cursor_text) if cursor_text.isdigit() else 0
        cursor = max(0, cursor)
        requested_limit = max(1, int(args.get("limit") or 3))
        job_id = str(args.get("job_id") or "")
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

        context = _source_ref_context(source_inventory.source_cards)
        brief = workspace.get("corpus_brief") if isinstance(workspace.get("corpus_brief"), dict) else {}
        brief_field_by_section = {
            "corpus_brief_questions": "coverage_questions",
            "corpus_brief_findings": "source_findings",
            "corpus_brief_outcomes": "coverage_outcomes",
            "corpus_brief_handoff": "research_handoff",
            "corpus_brief_gap_records": "gap_records",
        }
        if section in brief_field_by_section:
            values = [
                item
                for item in brief.get(brief_field_by_section[section]) or []
                if isinstance(item, dict)
            ]
            page_values = values[cursor : cursor + requested_limit]
            items = [
                _audit_corpus_brief_page_item(
                    section,
                    item,
                    context,
                    index=cursor + offset,
                )
                for offset, item in enumerate(page_values, start=1)
            ]
            return fit_model_page(
                {
                    "accepted": True,
                    "section": section,
                    "items": items,
                    "page": _page_metadata(total=len(values), cursor=cursor, returned=len(items)),
                },
                requested_limit=requested_limit,
            )

        investigation_jobs = [
            item for item in workspace.get("investigation_jobs") or [] if isinstance(item, dict)
        ]
        if section in {"jobs", "job_questions", "job_question_outcomes"}:
            if section == "jobs":
                values = investigation_jobs
            elif section == "job_questions":
                values = [
                    (str(job.get("job_id") or ""), question)
                    for job in investigation_jobs
                    if not job_id or str(job.get("job_id") or "") == job_id
                    for question in job.get("questions") or []
                ]
            else:
                values = [
                    (str(job.get("job_id") or ""), outcome)
                    for job in investigation_jobs
                    if not job_id or str(job.get("job_id") or "") == job_id
                    for outcome in job.get("question_outcomes") or []
                    if isinstance(outcome, dict)
                ]
            page_values = values[cursor : cursor + requested_limit]
            items = [
                _audit_job_page_item(
                    section,
                    item,
                    cursor + offset,
                    evidence_ref_by_id=evidence_ref_by_id,
                    required_evidence_refs=required_evidence_refs,
                )
                for offset, item in enumerate(page_values)
            ]
            return fit_model_page(
                {
                    "accepted": True,
                    "section": section,
                    "items": items,
                    "page": _page_metadata(total=len(values), cursor=cursor, returned=len(items)),
                },
                requested_limit=requested_limit,
            )

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
        elif section == "workspace_risks":
            values = workspace_risks
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
        return fit_model_page(
            {
                "accepted": True,
                "section": section,
                "items": items,
                "page": _page_metadata(total=len(values), cursor=cursor, returned=len(items)),
            },
            requested_limit=limit,
        )

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
                        "jobs",
                        "job_questions",
                        "job_question_outcomes",
                        "corpus_brief_questions",
                        "corpus_brief_findings",
                        "corpus_brief_outcomes",
                        "corpus_brief_handoff",
                        "corpus_brief_gap_records",
                        "source_map_cards",
                        "source_map_quotes",
                        "source_map_clusters",
                        "source_map_relationships",
                        "source_map_gaps",
                        "source_map_gap_records",
                    ],
                },
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                "job_id": {
                    "type": "string",
                    "description": "Use when paging one job's questions or outcomes; omit otherwise.",
                },
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
    evidence_previews_by_atom_ref, evidence_id_by_ref, required_evidence_refs = audit_evidence_previews(
        research_memo=research_memo,
        evidence_records=evidence_records or [],
        context=context,
        source_titles_by_id=source_titles_by_id,
        workspace=workspace,
    )
    evidence_ref_by_id: dict[str, str] = {}
    for evidence_ref, evidence_id in evidence_id_by_ref.items():
        evidence_ref_by_id.setdefault(evidence_id, evidence_ref)
    atom_drafts = [item for item in workspace.get("atom_drafts") or [] if isinstance(item, dict)]
    active_drafts = [item for item in atom_drafts if not item.get("merged_into")]
    atom_limit = 12
    list_limit = 20
    memo_atoms = list(research_memo.editorial_atoms)
    processed_jobs = [item for item in workspace.get("processed_jobs") or [] if isinstance(item, dict)]
    investigation_jobs = [
        item for item in workspace.get("investigation_jobs") or [] if isinstance(item, dict)
    ]
    job_question_count = sum(len(item.get("questions") or []) for item in investigation_jobs)
    job_question_outcome_count = sum(
        len(item.get("question_outcomes") or []) for item in investigation_jobs
    )
    job_outcome_evidence_refs = {
        evidence_ref
        for job in investigation_jobs
        for outcome in job.get("question_outcomes") or []
        if isinstance(outcome, dict)
        for evidence_ref in _outcome_evidence_refs(outcome, evidence_ref_by_id)
    }
    research_gaps = [item for item in workspace.get("research_gaps") or [] if isinstance(item, dict)]
    workspace_risks = list(workspace.get("risk_register") or [])
    brief = workspace.get("corpus_brief") if isinstance(workspace.get("corpus_brief"), dict) else {}
    source_map_summary = source_map_research_summary(source_map, inventory, include_evidence_ids=False)
    # CorpusBrief is the authoritative triage handoff. Keep SourceMap gap
    # records pageable for investigation, but do not duplicate them in the
    # initial audit payload and make one gap look like two independent issues.
    source_map_summary.pop("gap_records", None)
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
            "investigation_jobs": [
                _investigation_job_preview(
                    item,
                    evidence_ref_by_id=evidence_ref_by_id,
                    required_evidence_refs=required_evidence_refs,
                )
                for item in investigation_jobs[:list_limit]
            ],
            "investigation_job_count": len(investigation_jobs),
            "job_outcome_evidence_ref_count": len(job_outcome_evidence_refs),
            "required_job_outcome_evidence_ref_count": len(
                job_outcome_evidence_refs & required_evidence_refs
            ),
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
                "jobs": _page_metadata(
                    total=len(investigation_jobs),
                    cursor=0,
                    returned=min(len(investigation_jobs), list_limit),
                ),
                "job_questions": _page_metadata(
                    total=job_question_count,
                    cursor=0,
                    returned=0,
                ),
                "job_question_outcomes": _page_metadata(
                    total=job_question_outcome_count,
                    cursor=0,
                    returned=0,
                ),
                "research_gaps": _page_metadata(
                    total=len(research_gaps), cursor=0, returned=min(len(research_gaps), list_limit)
                ),
                "workspace_risks": _page_metadata(
                    total=len(workspace_risks), cursor=0, returned=min(len(workspace_risks), list_limit)
                ),
            },
        },
        "corpus_brief": _corpus_brief_preview_for_audit(brief, context, limit=list_limit),
        "source_map_summary": source_map_summary,
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


def normalize_research_audit_references(
    audit: ResearchAudit,
    *,
    context: Any,
    evidence_id_by_ref: dict[str, str],
) -> ResearchAudit:
    """Resolve model-facing audit refs against the current run inventory.

    The model knows public S/E refs, while repair stores internal evidence ids.
    Unknown handles are removed instead of creating an impossible narrow job.
    """

    evidence_ref_by_id: dict[str, str] = {}
    for evidence_ref, evidence_id in evidence_id_by_ref.items():
        evidence_ref_by_id.setdefault(evidence_id, evidence_ref)
    for issue in audit.issues:
        normalized: list[str] = []
        invalid_count = 0
        for value in issue.source_refs:
            text = str(value or "").strip()
            source_ref = context.ref_by_source_id.get(text)
            if source_ref is None:
                public_candidate = text.upper()
                if public_candidate in context.source_id_by_ref:
                    source_ref = public_candidate
            if source_ref is None:
                invalid_count += 1
                continue
            if source_ref not in normalized:
                normalized.append(source_ref)
        issue.source_refs = normalized
        if invalid_count:
            notice = (
                f"Backend removed {invalid_count} unknown source reference(s); "
                "repair must re-establish source scope from the current corpus."
            )
            issue.suggested_repair = " ".join(
                value for value in [issue.suggested_repair.strip(), notice] if value
            )

        normalized_evidence_refs: list[str] = []
        normalized_evidence_ids: list[str] = []
        invalid_evidence_count = 0
        for value in [*issue.evidence_refs, *issue.evidence_ids]:
            text = str(value or "").strip()
            evidence_ref = evidence_ref_by_id.get(text)
            if evidence_ref is not None:
                evidence_id = text
            else:
                evidence_ref = text.upper()
                evidence_id = evidence_id_by_ref.get(evidence_ref)
            if evidence_id is None or evidence_ref is None:
                invalid_evidence_count += 1
                continue
            if evidence_ref not in normalized_evidence_refs:
                normalized_evidence_refs.append(evidence_ref)
            if evidence_id not in normalized_evidence_ids:
                normalized_evidence_ids.append(evidence_id)
        issue.evidence_refs = normalized_evidence_refs
        issue.evidence_ids = normalized_evidence_ids
        if invalid_evidence_count:
            notice = (
                f"Backend removed {invalid_evidence_count} unknown evidence reference(s); "
                "repair must re-establish grounding from current evidence."
            )
            issue.suggested_repair = " ".join(
                value for value in [issue.suggested_repair.strip(), notice] if value
            )
    return audit

__all__ = [
    "audit_evidence_catalog",
    "audit_evidence_previews",
    "audit_tools_with_evidence_refs",
    "audit_workspace_page_tool",
    "evidence_preview_snippet",
    "evidence_support_text",
    "normalize_research_audit",
    "normalize_research_audit_references",
    "research_audit_input",
    "strip_audit_backend_ids",
]
