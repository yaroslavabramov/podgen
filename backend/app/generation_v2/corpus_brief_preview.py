from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.schemas_source import CorpusBrief, SourceMap
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref_for_record


def semantic_preview(
    value: str,
    *,
    limit: int,
    replacements: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    text = model_preview_text(value, replacements=replacements, collapse_whitespace=False)
    return {
        "preview": text[:limit],
        "chars": len(text),
        "truncated": len(text) > limit,
    }


def corpus_brief_preview(
    brief: CorpusBrief,
    *,
    source_inventory: SourceMap | None = None,
    item_limit: int = 20,
    text_limit: int = 400,
) -> dict[str, Any]:
    questions = brief.coverage_questions[:item_limit]
    findings = brief.source_findings[:item_limit]
    # Initial-reading groups are operational provenance, not research branches.
    content_source_sets = [item for item in brief.source_sets if str(item.set_kind) != "orientation"]
    source_sets = content_source_sets[:item_limit]
    outcomes = brief.coverage_outcomes[:item_limit]
    handoff = brief.research_handoff[:item_limit]
    source_ref_context = build_source_ref_context(source_inventory) if source_inventory is not None else None
    replacements = source_ref_context.ref_by_source_id if source_ref_context is not None else None
    gap_records = brief.gap_records[:item_limit]
    structured_gap_keys = {
        " ".join(str(item.description).split()).casefold() for item in brief.gap_records
    }
    model_cautions = [
        item
        for item in brief.cautions
        if " ".join(str(item).split()).casefold() not in structured_gap_keys
    ]
    cautions = model_cautions[:item_limit]

    def preview(value: str) -> dict[str, Any]:
        return semantic_preview(value, limit=text_limit, replacements=replacements)

    return {
        "schema_version": "corpus_brief_preview.v2",
        "corpus_brief_schema_version": brief.schema_version,
        "summary": preview(brief.summary),
        "intent": {
            "user_prompt": preview(brief.intent.user_prompt),
            "central_question": preview(brief.intent.central_question),
            "language": brief.intent.language,
            "duration_seconds": brief.intent.duration_seconds,
            "constraint_previews": [
                preview(item) for item in brief.intent.constraints[:item_limit]
            ],
            "constraint_count": len(brief.intent.constraints),
            "constraint_omitted_count": max(0, len(brief.intent.constraints) - item_limit),
        },
        "coverage_questions": [
            {
                "question_id": item.question_id,
                "kind": item.kind,
                "importance": item.importance,
                "question": preview(item.question),
                "rationale": preview(item.rationale),
                "answer_requirements": [
                    {
                        "requirement_id": requirement.requirement_id,
                        "description": preview(requirement.description),
                        "rationale": preview(requirement.rationale),
                        "completion_criterion": preview(requirement.completion_criterion),
                    }
                    for requirement in item.answer_requirements[:item_limit]
                ],
                "answer_requirement_count": len(item.answer_requirements),
                "answer_requirement_omitted_count": max(
                    0, len(item.answer_requirements) - item_limit
                ),
            }
            for item in questions
        ],
        "source_findings": [
            {
                "finding_id": item.finding_id,
                **(
                    {"source_ref": source_ref_for_id(item.source_id, source_ref_context)}
                    if source_ref_context is not None
                    else {}
                ),
                "evidence_count": len(item.evidence_ids),
                "semantic_revision": item.semantic_revision,
                "statement": preview(item.statement),
                "support_explanation": preview(item.support_explanation),
                "scope": {
                    "actor": preview(item.scope.actor),
                    "period": preview(item.scope.period),
                    "geography": preview(item.scope.geography),
                    "qualifier_previews": [
                        preview(value) for value in item.scope.qualifiers[:item_limit]
                    ],
                    "qualifier_count": len(item.scope.qualifiers),
                },
                "question_links": [
                    {
                        "question_id": link.question_id,
                        "requirement_ids": list(link.requirement_ids),
                        "rationale": preview(link.rationale),
                    }
                    for link in item.question_links[:item_limit]
                ],
                "question_link_count": len(item.question_links),
                "limitation_previews": [
                    preview(value) for value in item.limitations[:item_limit]
                ],
                "limitation_count": len(item.limitations),
                "source_relation_count": len(item.source_relations),
            }
            for item in findings
        ],
        "source_sets": [
            {
                "set_id": item.set_id,
                "set_kind": item.set_kind,
                "status": item.status,
                "priority": item.priority,
                "source_count": len(item.source_ids),
                "question_count": len(item.question_ids),
                "evidence_count": len(item.evidence_ids),
                "label": preview(item.label),
                "purpose": preview(item.purpose),
                "rationale": preview(item.rationale),
                "findings_summary": preview(item.findings_summary),
                "deferred_reason": preview(item.deferred_reason),
            }
            for item in source_sets
        ],
        "coverage_outcomes": [
            {
                "outcome_id": item.outcome_id,
                "question_id": item.question_id,
                "status": item.status,
                "source_count": len(item.source_ids),
                "evidence_count": len(item.evidence_ids),
                "finding_ids": list(item.finding_ids[:item_limit]),
                "finding_count": len(item.finding_ids),
                "requirement_results": [
                    {
                        "requirement_id": result.requirement_id,
                        "status": result.status,
                        "finding_ids": list(result.finding_ids[:item_limit]),
                        "finding_count": len(result.finding_ids),
                        "summary": preview(result.summary),
                        "closure_basis": preview(result.closure_basis),
                        "remaining_uncertainty": preview(result.remaining_uncertainty),
                        "missing_detail": preview(result.missing_detail),
                        "conflict_summary": preview(result.conflict_summary),
                        "deferred_reason": preview(result.deferred_reason),
                    }
                    for result in item.requirement_results[:item_limit]
                ],
                "requirement_result_count": len(item.requirement_results),
                "semantic_revision": item.semantic_revision,
                "is_stale": item.is_stale,
                "orientation_summary": preview(item.orientation_summary),
                "missing_detail": preview(item.missing_detail),
                "conflict_summary": preview(item.conflict_summary),
                "deferred_reason": preview(item.deferred_reason),
            }
            for item in outcomes
        ],
        "research_handoff": [
            {
                "handoff_id": item.handoff_id,
                "priority": item.priority,
                "action": item.action,
                "question_ids": item.question_ids,
                "question_assignments": [
                    assignment.model_dump(mode="json")
                    for assignment in item.question_assignments
                ],
                "finding_ids": list(item.finding_ids[:item_limit]),
                "finding_count": len(item.finding_ids),
                "preferred_source_count": len(item.preferred_source_ids),
                "starting_evidence_count": len(item.starting_evidence_ids),
                "objective": preview(item.objective),
                "reason": preview(item.reason),
            }
            for item in handoff
        ],
        "gap_records": [
            {
                "gap_ref": public_gap_ref_for_record(item),
                "kind": item.kind,
                **(
                    {
                        "source_refs": [
                            source_ref_for_id(source_id, source_ref_context)
                            for source_id in item.source_ids
                        ]
                    }
                    if source_ref_context is not None
                    else {"source_count": len(item.source_ids)}
                ),
                "evidence_count": len(item.evidence_ids),
                "description": preview(item.description),
            }
            for item in gap_records
        ],
        "caution_previews": [preview(item) for item in cautions],
        "caution_count": len(model_cautions),
        "pages": {
            "corpus_brief_summary": _page(1, 0 if len(brief.summary) > text_limit else 1),
            "corpus_brief_intent": _page(
                1,
                0
                if _intent_is_truncated(brief, text_limit=text_limit, item_limit=item_limit)
                else 1,
            ),
            "corpus_brief_cautions": _page(len(model_cautions), len(cautions)),
            "corpus_brief_questions": _page(
                len(brief.coverage_questions),
                len(questions),
                full_page_unit="record_part",
            ),
            "corpus_brief_findings": _page(
                len(brief.source_findings),
                len(findings),
                full_page_unit="record_part",
            ),
            "corpus_brief_source_sets": _page(len(content_source_sets), len(source_sets)),
            "corpus_brief_outcomes": _page(
                len(brief.coverage_outcomes),
                len(outcomes),
                full_page_unit="record_part",
            ),
            "corpus_brief_handoff": _page(len(brief.research_handoff), len(handoff)),
            "corpus_brief_gap_records": _page(len(brief.gap_records), len(gap_records)),
        },
    }


def _page(
    total: int,
    returned: int,
    *,
    full_page_unit: str = "record",
) -> dict[str, Any]:
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - returned),
        "cursor": "0",
        "next_cursor": str(returned) if returned < total else "",
        "cursor_unit": "preview_record",
        "full_page_start_cursor": "0",
        "full_page_unit": full_page_unit,
    }


def _intent_is_truncated(brief: CorpusBrief, *, text_limit: int, item_limit: int) -> bool:
    return len(brief.intent.constraints) > item_limit or any(
        len(model_preview_text(value, collapse_whitespace=False)) > text_limit
        for value in [
            brief.intent.user_prompt,
            brief.intent.central_question,
            *brief.intent.constraints,
        ]
    )


__all__ = ["corpus_brief_preview", "semantic_preview"]
