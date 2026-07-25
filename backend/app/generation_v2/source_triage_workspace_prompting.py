from __future__ import annotations

from typing import Any

from app.generation_v2.model_context_text import (
    is_generic_window_locator,
    model_preview_text,
    stable_unique_texts,
)
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_model_delivery import source_structure_coverage
from app.generation_v2.source_triage_workspace_state import (
    SOURCE_SET_KIND_COVERAGE,
    SOURCE_SET_KIND_ORIENTATION,
    latest_source_read_receipts,
    source_read_coverage_ledger,
    source_read_receipt_delivery_status,
    source_read_receipt_is_delivered,
    uncheckpointed_source_read_replays,
    normalize_workspace as _normalize_workspace,
    source_set_has_unresolved_refs as _source_set_has_unresolved_refs,
    workspace_set as _workspace_set,
)
from app.generation_v2.source_triage_read_checkpointing import (
    pending_comparison_source_refs,
    pending_exact_read_source_refs,
    source_refs_with_reads_newer_than_analysis,
)
from app.generation_v2.source_triage_question_state import question_outcome_is_current
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id


PLANNER_CATALOG_TITLE_CHARS = 140
PLANNER_CATALOG_SUMMARY_CHARS = 180
PLANNER_CATALOG_CAUTION_CHARS = 140
PLANNER_CATALOG_CAUTION_LIMIT = 1
PLANNER_CATALOG_DOMAIN_CHARS = 120
_MISSING_PUBLICATION_DATE_WARNINGS = {
    "publication date is missing.",
    "published date is missing.",
}


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = model_preview_text(value, collapse_whitespace=False)
    return {
        f"{name}_preview": text[:limit],
        f"{name}_chars": len(text),
        f"{name}_truncated": len(text) > limit,
    }


def _lean_text_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = model_preview_text(value, collapse_whitespace=False)
    preview = text[:limit]
    payload: dict[str, Any] = {name: preview}
    omitted = max(0, len(text) - len(preview))
    if omitted:
        payload[f"{name}_omitted_chars"] = omitted
    return payload


def _lean_stored_text_fields(
    name: str,
    value: Any,
    *,
    chars: Any,
    limit: int,
) -> dict[str, Any]:
    text = model_preview_text(value, collapse_whitespace=False)
    preview = text[:limit]
    original_chars = int(chars or len(text))
    payload: dict[str, Any] = {name: preview}
    omitted = max(0, original_chars - len(preview))
    if omitted:
        payload[f"{name}_omitted_chars"] = omitted
    return payload


def _checkpoint_intent_payload(workspace: dict[str, Any]) -> dict[str, Any]:
    intent = workspace.get("intent") or {}
    return {
        "language": model_preview_text(intent.get("language"))[:40] or None,
        "duration_seconds": int(intent.get("duration_seconds") or 0),
        **_preview_fields("user_prompt", intent.get("user_prompt") or "", 800),
    }


def planner_resume_payload(workspace: dict[str, Any]) -> dict[str, Any]:
    """Describe accepted planner writes and the next valid transition.

    This is deliberately derived from the canonical workspace instead of the
    previous model conversation. It therefore remains correct after both a
    Responses-chain checkpoint and a worker restart.
    """

    workspace = _normalize_workspace(workspace)
    source_order = [str(item) for item in workspace.get("source_order") or []]
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    source_sets = [item for item in workspace.get("source_sets") or [] if isinstance(item, dict)]
    known_question_ids = {
        str(item.get("question_id") or "") for item in questions if str(item.get("question_id") or "")
    }
    assignment_counts = {
        source_ref: sum(source_ref in (source_set.get("source_refs") or []) for source_set in source_sets)
        for source_ref in source_order
    }
    assigned_question_ids = {
        str(question_id)
        for source_set in source_sets
        if str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_COVERAGE
        for question_id in source_set.get("planned_question_ids", source_set.get("question_ids") or [])
        if str(question_id)
    }
    missing_source_refs = [source_ref for source_ref, count in assignment_counts.items() if count == 0]
    duplicate_source_refs = [source_ref for source_ref, count in assignment_counts.items() if count > 1]
    unassigned_question_ids = sorted(known_question_ids - assigned_question_ids)
    unknown_question_ids = sorted(assigned_question_ids - known_question_ids)
    source_set_ids_without_questions = [
        str(item.get("set_id") or "")
        for item in source_sets
        if str(item.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_COVERAGE
        and not (item.get("planned_question_ids", item.get("question_ids") or []))
    ]
    orientation_set_ids_with_planned_questions = [
        str(item.get("set_id") or "")
        for item in source_sets
        if str(item.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_ORIENTATION
        and (item.get("planned_question_ids") or item.get("question_ids"))
    ]
    next_action = _planner_next_action(
        status=str(workspace.get("status") or ""),
        question_ids=sorted(known_question_ids),
        source_set_count=len(source_sets),
        missing_source_refs=missing_source_refs,
        duplicate_source_refs=duplicate_source_refs,
        unassigned_question_ids=unassigned_question_ids,
        unknown_question_ids=unknown_question_ids,
        source_set_ids_without_questions=source_set_ids_without_questions,
        orientation_set_ids_with_planned_questions=orientation_set_ids_with_planned_questions,
    )
    return {
        "accepted_writes_are_authoritative": True,
        "accepted_coverage_questions": [
            {
                "question_id": item.get("question_id"),
                **_preview_fields("question", item.get("question") or "", 280),
                "kind": item.get("kind"),
                "importance": item.get("importance"),
                **_preview_fields("rationale", item.get("rationale") or "", 400),
                "parent_question_id": item.get("parent_question_id"),
            }
            for item in questions
        ],
        "accepted_coverage_question_count": len(questions),
        "accepted_source_sets": [
            {
                "set_id": item.get("set_id"),
                "set_kind": item.get("set_kind") or SOURCE_SET_KIND_COVERAGE,
                **_preview_fields("label", item.get("label") or "", 180),
                "source_refs": [str(ref) for ref in item.get("source_refs") or []],
                "question_ids": [str(question_id) for question_id in item.get("question_ids") or []],
                "status": item.get("status"),
            }
            for item in source_sets
        ],
        "accepted_source_set_count": len(source_sets),
        "assigned_source_ref_count": sum(count > 0 for count in assignment_counts.values()),
        "missing_source_refs": missing_source_refs,
        "duplicate_source_refs": duplicate_source_refs,
        "unassigned_question_ids": unassigned_question_ids,
        "unknown_question_ids": unknown_question_ids,
        "source_set_ids_without_questions": source_set_ids_without_questions,
        "orientation_set_ids_with_planned_questions": orientation_set_ids_with_planned_questions,
        "central_question_id": workspace.get("central_question_id"),
        "ready_to_finalize": bool(
            questions
            and source_sets
            and not missing_source_refs
            and not duplicate_source_refs
            and not unassigned_question_ids
            and not unknown_question_ids
            and not source_set_ids_without_questions
            and not orientation_set_ids_with_planned_questions
        ),
        "next_action": next_action,
    }


def _planner_next_action(
    *,
    status: str,
    question_ids: list[str],
    source_set_count: int,
    missing_source_refs: list[str],
    duplicate_source_refs: list[str],
    unassigned_question_ids: list[str],
    unknown_question_ids: list[str],
    source_set_ids_without_questions: list[str],
    orientation_set_ids_with_planned_questions: list[str],
) -> dict[str, Any]:
    if status == "planned":
        return {
            "tool_name": "finalize_triage_workspace_plan",
            "reason": "The durable plan is already complete; the terminal resume loader should return it.",
        }
    if not question_ids:
        return {
            "tool_name": "upsert_coverage_question",
            "reason": "No coverage question has been accepted yet.",
        }
    if (
        duplicate_source_refs
        or unknown_question_ids
        or source_set_ids_without_questions
        or orientation_set_ids_with_planned_questions
    ):
        return {
            "tool_name": "upsert_triage_source_set",
            "reason": "Repair the concrete invalid assignments without recreating accepted questions.",
            "duplicate_source_refs": duplicate_source_refs,
            "unknown_question_ids": unknown_question_ids,
            "source_set_ids_without_questions": source_set_ids_without_questions,
            "orientation_set_ids_with_planned_questions": orientation_set_ids_with_planned_questions,
        }
    if missing_source_refs:
        return {
            "tool_name": "upsert_triage_source_set",
            "alternative_tool_name": "route_sources_for_initial_reading",
            "reason": (
                "Continue substantive source assignment; if an explicitly inspected source still cannot be "
                "attached honestly, route it for initial reading."
            ),
            "source_refs_to_assign": missing_source_refs,
            "accepted_question_ids": question_ids,
            "existing_source_set_count": source_set_count,
        }
    if unassigned_question_ids:
        return {
            "tool_name": "upsert_triage_source_set",
            "reason": "Attach the remaining accepted questions to an appropriate existing or new source set.",
            "question_ids_to_assign": unassigned_question_ids,
        }
    return {
        "tool_name": "finalize_triage_workspace_plan",
        "reason": "Every source and accepted question has exactly the required assignment.",
    }


def _stored_preview_fields(
    name: str,
    value: Any,
    *,
    chars: Any,
    truncated: Any,
    limit: int,
) -> dict[str, Any]:
    text = model_preview_text(value, collapse_whitespace=False)
    original_chars = int(chars or len(text))
    return {
        f"{name}_preview": text[:limit],
        f"{name}_chars": original_chars,
        f"{name}_truncated": bool(truncated) or original_chars > len(text[:limit]),
    }


def planner_source_catalog(inventory: SourceMap) -> dict[str, Any]:
    """Return one bounded orientation row for every source in the run."""

    context = build_source_ref_context(inventory)
    sources: list[dict[str, Any]] = []
    source_refs_by_metadata_flag: dict[str, list[str]] = {}
    for card in inventory.source_cards:
        source_ref = source_ref_for_id(card.source_id, context)
        replacements = {
            internal_id: source_ref
            for internal_id in [
                str(card.source_id),
                *(str(item) for item in (card.source_version_ids or [])),
                *(str(item) for item in (card.representation_ids or [])),
                str(card.source_profile_id or ""),
            ]
            if internal_id
        }
        title = model_preview_text(card.title, replacements=replacements)
        domain = model_preview_text(card.domain, replacements=replacements)
        summary = model_preview_text(card.summary, replacements=replacements)
        cautions = stable_unique_texts(
            model_preview_text(item, replacements=replacements)
            for item in card.gaps_or_cautions
        )
        metadata_flags = [
            "missing_publication_date"
            for item in cautions
            if item.casefold() in _MISSING_PUBLICATION_DATE_WARNINGS
        ]
        for flag in stable_unique_texts(metadata_flags):
            source_refs_by_metadata_flag.setdefault(flag, []).append(source_ref)
        content_cautions = [
            item
            for item in cautions
            if item.casefold() not in _MISSING_PUBLICATION_DATE_WARNINGS
            and not is_generic_window_locator(item)
        ]
        caution_previews = [
            {
                "preview": item[:PLANNER_CATALOG_CAUTION_CHARS],
                "omitted_chars": max(0, len(item) - PLANNER_CATALOG_CAUTION_CHARS),
            }
            for item in content_cautions[:PLANNER_CATALOG_CAUTION_LIMIT]
        ]
        sources.append(
            {
                "source_ref": source_ref,
                "title_preview": title[:PLANNER_CATALOG_TITLE_CHARS],
                "title_omitted_chars": max(0, len(title) - PLANNER_CATALOG_TITLE_CHARS),
                "domain": domain[:PLANNER_CATALOG_DOMAIN_CHARS],
                "domain_omitted_chars": max(0, len(domain) - PLANNER_CATALOG_DOMAIN_CHARS),
                "source_type": model_preview_text(card.source_type)[:40],
                "published_at": model_preview_text(card.published_at)[:40] or None,
                "coverage_status": model_preview_text(card.coverage_status)[:40],
                "summary_preview": summary[:PLANNER_CATALOG_SUMMARY_CHARS],
                "summary_omitted_chars": max(0, len(summary) - PLANNER_CATALOG_SUMMARY_CHARS),
                "caution_previews": caution_previews,
                "caution_omitted_count": max(0, len(content_cautions) - len(caution_previews)),
            }
        )
    return {
        "schema_version": "source_triage_planner_catalog.v1",
        "complete_source_list": True,
        "source_count": len(sources),
        "preview_contract": "A positive omitted_chars value means that preview is partial.",
        "source_refs_by_metadata_flag": source_refs_by_metadata_flag,
        "sources": sources,
    }


def _observation_previews(source_set: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    observations = [item for item in source_set.get("observations") or [] if isinstance(item, dict)]
    return [
        {
            "source_refs": item.get("source_refs") or [],
            "evidence_ids": (item.get("evidence_ids") or [])[:8],
            "evidence_count": len(item.get("evidence_ids") or []),
            "evidence_omitted_count": max(0, len(item.get("evidence_ids") or []) - 8),
            **_preview_fields("note", item.get("note") or "", 500),
            "pending_question_previews": [
                _preview_fields("text", question, 220) for question in (item.get("pending_questions") or [])[:8]
            ],
            "pending_question_count": len(item.get("pending_questions") or []),
        }
        for item in observations[-limit:]
    ]


def _written_action_previews(source_set: dict[str, Any], *, limit: int = 16) -> list[dict[str, Any]]:
    written_actions = [item for item in source_set.get("written_actions") or [] if isinstance(item, dict)]
    return [
        {
            "tool_name": item.get("tool_name"),
            "source_refs": item.get("source_refs") or [],
            "evidence_ids": (item.get("evidence_ids") or [])[:8],
            "evidence_count": len(item.get("evidence_ids") or []),
            "evidence_omitted_count": max(0, len(item.get("evidence_ids") or []) - 8),
            "created_at": item.get("created_at"),
        }
        for item in written_actions[-limit:]
    ]


def _capsule_prompt_preview(capsule: dict[str, Any]) -> dict[str, Any]:
    outline = list(capsule.get("outline") or [])
    outline_item_chars = list(capsule.get("outline_item_chars") or [])
    windows = [item for item in capsule.get("windows") or [] if isinstance(item, dict)]
    warnings = list(capsule.get("readability_warnings") or [])
    warning_item_chars = list(capsule.get("readability_warning_item_chars") or [])
    return {
        "source_ref": capsule.get("source_ref"),
        **_lean_stored_text_fields("title", capsule.get("title") or "", chars=capsule.get("title_chars"), limit=180),
        **_lean_stored_text_fields("domain", capsule.get("domain") or "", chars=capsule.get("domain_chars"), limit=180),
        "source_type": capsule.get("source_type"),
        "published_at": capsule.get("published_at"),
        "coverage_status": capsule.get("coverage_status"),
        "text_available": bool(capsule.get("text_available", capsule.get("readable"))),
        **_lean_stored_text_fields(
            "summary_hint",
            capsule.get("summary_hint") or "",
            chars=capsule.get("summary_hint_chars"),
            limit=180,
        ),
        "outline": [
            _lean_stored_text_fields(
                "text",
                item,
                chars=outline_item_chars[index] if index < len(outline_item_chars) else None,
                limit=100,
            )
            for index, item in enumerate(outline[:4])
        ],
        "outline_count": max(len(outline), int(capsule.get("outline_total_count") or 0)),
        "outline_omitted_count": max(
            0,
            max(len(outline), int(capsule.get("outline_total_count") or 0)) - min(len(outline), 4),
        ),
        "windows": [
            {
                **_lean_stored_text_fields(
                    "locator",
                    item.get("locator") or "",
                    chars=item.get("locator_chars"),
                    limit=100,
                ),
                "locator_type": item.get("locator_type"),
                **_lean_stored_text_fields(
                    "text",
                    item.get("preview_text") or "",
                    chars=item.get("preview_text_chars"),
                    limit=300,
                ),
            }
            for item in windows[:2]
        ],
        "window_count": len(windows),
        "readability_warnings": [
            _lean_stored_text_fields(
                "text",
                item,
                chars=warning_item_chars[index] if index < len(warning_item_chars) else None,
                limit=300,
            )
            for index, item in enumerate(warnings[:4])
        ],
        "readability_warning_count": max(
            len(warnings),
            int(capsule.get("readability_warning_total_count") or 0),
        ),
        "readability_warning_omitted_count": max(
            0,
            max(len(warnings), int(capsule.get("readability_warning_total_count") or 0))
            - min(len(warnings), 4),
        ),
    }


def workspace_summary_for_prompt(
    workspace: dict[str, Any],
    *,
    max_sets: int = 30,
    set_cursor: int = 0,
    max_deferred_sources: int = 20,
    deferred_source_cursor: int = 0,
    include_planner_resume: bool = False,
) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    source_sets = list(workspace.get("source_sets") or [])
    set_page = source_sets[set_cursor : set_cursor + max_sets]
    deferred_sources = [item for item in workspace.get("deferred_sources") or [] if isinstance(item, dict)]
    deferred_page = deferred_sources[deferred_source_cursor : deferred_source_cursor + max_deferred_sources]
    coverage_questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    source_assessments = [item for item in workspace.get("source_assessments") or [] if isinstance(item, dict)]
    source_findings = [item for item in workspace.get("source_findings") or [] if isinstance(item, dict)]
    coverage_outcomes = [item for item in workspace.get("coverage_outcomes") or [] if isinstance(item, dict)]
    coverage_syntheses = [item for item in workspace.get("coverage_syntheses") or [] if isinstance(item, dict)]
    source_map_repair_items = [
        item for item in workspace.get("source_map_repair_items") or [] if isinstance(item, dict)
    ]
    payload = {
        "status": workspace.get("status"),
        "intent": workspace.get("intent") or {},
        **_preview_fields("plan_summary", workspace.get("plan_summary") or "", 500),
        "source_count": len(workspace.get("source_order") or []),
        "source_set_count": len(workspace.get("source_sets") or []),
        "coverage_questions": [
            {
                "question_id": item.get("question_id"),
                **_preview_fields("question", item.get("question") or "", 280),
                "kind": item.get("kind"),
                "importance": item.get("importance"),
                **_preview_fields("rationale", item.get("rationale") or "", 400),
                "parent_question_id": item.get("parent_question_id"),
                "answer_requirement_count": len(item.get("answer_requirements") or []),
                "answer_requirement_previews": [
                    {
                        "requirement_id": requirement.get("requirement_id"),
                        **_preview_fields(
                            "description",
                            requirement.get("description") or "",
                            220,
                        ),
                    }
                    for requirement in item.get("answer_requirements") or []
                    if isinstance(requirement, dict)
                ],
            }
            for item in coverage_questions
        ],
        "coverage_question_count": len(coverage_questions),
        "source_assessment_count": len(source_assessments),
        "source_finding_count": len(source_findings),
        "source_finding_previews": [
            {
                "finding_id": item.get("finding_id"),
                **_preview_fields("statement", item.get("statement") or "", 300),
                "question_ids": [
                    link.get("question_id")
                    for link in item.get("question_links") or []
                    if isinstance(link, dict)
                ],
                "evidence_count": len(item.get("evidence_ids") or []),
            }
            for item in source_findings[:20]
        ],
        "source_finding_omitted_count": max(0, len(source_findings) - 20),
        "source_assessment_statuses": [
            {
                "source_ref": item.get("source_ref"),
                "processing_status": item.get("processing_status"),
                "representation_status": item.get("representation_status"),
                "relevance": item.get("relevance"),
                "disposition": item.get("disposition"),
            }
            for item in source_assessments
        ],
        "coverage_outcome_count": len(coverage_outcomes),
        "coverage_outcome_statuses": [
            {
                "question_id": item.get("question_id"),
                "set_id": item.get("set_id"),
                "status": item.get("status"),
            }
            for item in coverage_outcomes
        ],
        "coverage_synthesis_count": len(coverage_syntheses),
        "coverage_synthesis_statuses": [
            {
                "question_id": item.get("question_id"),
                "status": item.get("status"),
                "next_action": item.get("next_action"),
            }
            for item in coverage_syntheses
        ],
        "source_map_repair_item_count": len(source_map_repair_items),
        "source_map_repair_item_previews": [
            {
                "repair_ref": item.get("repair_ref"),
                "status": item.get("status"),
                "kind": item.get("kind"),
                "issues": item.get("issues") or [],
                "source_refs": item.get("source_refs") or [],
                "relationship": item.get("relationship"),
                "cluster_id": item.get("cluster_id"),
                "gap_ref": item.get("gap_ref"),
                "evidence_count": len(item.get("evidence_ids") or []),
                **_preview_fields(
                    "detail",
                    item.get("rationale") or item.get("description") or item.get("label") or "",
                    300,
                ),
            }
            for item in source_map_repair_items[:10]
        ],
        "source_map_repair_item_omitted_count": max(0, len(source_map_repair_items) - 10),
        "current_set": workspace.get("current_set"),
        "processed_sets": [
            {
                "set_id": item.get("set_id"),
                "status": item.get("status"),
                **_preview_fields("summary", item.get("summary") or "", 500),
            }
            for item in workspace.get("processed_sets") or []
            if isinstance(item, dict)
        ],
        "mapped_sources": workspace.get("mapped_sources") or [],
        "deferred_sources": [
            {
                "source_ref": item.get("source_ref"),
                **_preview_fields("reason", item.get("reason") or "", 400),
            }
            for item in deferred_page
        ],
        "deferred_source_count": len(deferred_sources),
        "deferred_sources_page": {
            "total_count": len(deferred_sources),
            "returned_count": len(deferred_page),
            "omitted_count": max(
                0,
                len(deferred_sources) - (deferred_source_cursor + len(deferred_page)),
            ),
            "cursor": str(deferred_source_cursor),
            "next_cursor": (
                str(deferred_source_cursor + len(deferred_page))
                if deferred_source_cursor + len(deferred_page) < len(deferred_sources)
                else ""
            ),
        },
        "open_question_previews": [
            _preview_fields("text", item, 400) for item in workspace.get("open_questions") or []
        ],
        "source_sets": [
            {
                "set_id": item.get("set_id"),
                "set_kind": item.get("set_kind") or SOURCE_SET_KIND_COVERAGE,
                **_preview_fields("label", item.get("label") or "", 180),
                **_preview_fields("purpose", item.get("purpose") or "", 400),
                "source_refs": item.get("source_refs") or [],
                "question_ids": item.get("question_ids") or [],
                "question_assignments": item.get("question_assignments") or [],
                "status": item.get("status"),
                "priority": item.get("priority"),
                **_preview_fields("findings_summary", item.get("findings_summary") or "", 600),
                "observation_count": len(item.get("observations") or []),
                "written_action_count": len(item.get("written_actions") or []),
                **_preview_fields("deferred_reason", item.get("deferred_reason") or "", 400),
            }
            for item in set_page
        ],
        "source_sets_page": {
            "total_count": len(source_sets),
            "returned_count": len(set_page),
            "omitted_count": max(0, len(source_sets) - (set_cursor + len(set_page))),
            "cursor": str(set_cursor),
            "next_cursor": str(set_cursor + len(set_page)) if set_cursor + len(set_page) < len(source_sets) else "",
        },
    }
    if include_planner_resume:
        payload["planner_resume"] = planner_resume_payload(workspace)
    return payload


def checkpoint_workspace_payload(workspace: dict[str, Any], *, max_sets: int = 40) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    summary = workspace_summary_for_prompt(workspace, max_sets=max_sets)
    current_set_pointer = workspace.get("current_set") or {}
    current_set_id = str(current_set_pointer.get("set_id") or "")
    current_set = _workspace_set(workspace, current_set_id) if current_set_id else None
    observations = list((current_set or {}).get("observations") or [])
    observation_previews = _observation_previews(current_set or {})
    written_actions = list((current_set or {}).get("written_actions") or [])
    written_action_previews = _written_action_previews(current_set or {})
    capsules = workspace.get("source_capsules") or {}
    current_capsule_previews = [
        _capsule_prompt_preview(capsule)
        for source_ref in (current_set or {}).get("source_refs") or []
        if isinstance((capsule := capsules.get(source_ref)), dict)
    ]
    current_set_summary = None
    if current_set is not None:
        execution_mode = _source_set_execution_mode(workspace, current_set, current_set_id)
        targeted_repair = execution_mode == "targeted_repair"
        current_set_summary = {
            "set_id": current_set.get("set_id"),
            "set_kind": current_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE,
            "status": current_set.get("status"),
            "source_refs": current_set.get("source_refs") or [],
            "priority": current_set.get("priority"),
            "execution_mode": execution_mode,
            "repair_source_refs": (current_set.get("repair_source_refs") or []) if targeted_repair else [],
            "repair_question_ids": (current_set.get("repair_question_ids") or []) if targeted_repair else [],
            "repair_targets": [
                {
                    "code": item.get("code"),
                    **_preview_fields("message", item.get("message") or "", 400),
                    "source_refs": item.get("source_refs") or [],
                    "question_ids": item.get("question_ids") or [],
                }
                for item in current_set.get("repair_targets") or []
                if targeted_repair and isinstance(item, dict)
            ],
            "started_at": current_set_pointer.get("started_at"),
            **_preview_fields("label", current_set.get("label") or "", 180),
            **_preview_fields("purpose", current_set.get("purpose") or "", 400),
            **_preview_fields("rationale", current_set.get("rationale") or "", 500),
            "question_ids": current_set.get("question_ids") or [],
            "question_count": len(current_set.get("question_ids") or []),
            "observation_count": len(observations),
            "observation_preview_count": len(observation_previews),
            "observation_omitted_count": max(0, len(observations) - len(observation_previews)),
            "written_action_count": len(written_actions),
            "written_action_preview_count": len(written_action_previews),
            "written_action_omitted_count": max(0, len(written_actions) - len(written_action_previews)),
        }
    return {
        "status": summary.get("status"),
        "intent": _checkpoint_intent_payload(workspace),
        "planner_resume": planner_resume_payload(workspace),
        "source_count": summary.get("source_count"),
        "source_set_count": summary.get("source_set_count"),
        "processed_set_count": len(workspace.get("processed_sets") or []),
        "mapped_source_count": len(workspace.get("mapped_sources") or []),
        "deferred_source_count": len(workspace.get("deferred_sources") or []),
        "open_question_count": len(workspace.get("open_questions") or []),
        "source_sets": summary.get("source_sets") or [],
        "source_sets_page": summary.get("source_sets_page") or {},
        "current_set": current_set_summary,
        "current_set_observation_previews": observation_previews,
        "current_set_written_action_previews": written_action_previews,
        "current_set_capsule_previews": current_capsule_previews,
    }


def unresolved_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "in_progress", "needs_repair"}
        or _source_set_has_unresolved_refs(workspace, item)
    ]


def unresolved_source_set_summary(workspace: dict[str, Any], *, limit: int | None = None) -> list[dict[str, Any]]:
    return [
        {
            "set_id": item.get("set_id"),
            "set_kind": item.get("set_kind") or SOURCE_SET_KIND_COVERAGE,
            "label": item.get("label"),
            "status": item.get("status"),
            "source_refs": item.get("source_refs") or [],
            "deferred_reason": item.get("deferred_reason") or "",
        }
        for item in (
            unresolved_source_sets(workspace)[:limit] if limit is not None else unresolved_source_sets(workspace)
        )
    ]


def _source_set_execution_mode(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    set_id: str,
) -> str:
    if isinstance(workspace.get("triage_semantic_eval"), dict):
        return "semantic_eval"
    if source_set.get("repair_targeted"):
        return "targeted_repair"
    has_durable_work = bool(
        source_set.get("observations")
        or source_set.get("written_actions")
        or source_set.get("evidence_ids")
        or any(
            isinstance(item, dict) and item.get("set_id") == set_id
            for item in workspace.get("source_assessments") or []
        )
        or any(
            isinstance(item, dict) and item.get("set_id") == set_id
            for item in workspace.get("source_findings") or []
        )
        or any(
            isinstance(item, dict) and item.get("set_id") == set_id
            for item in workspace.get("reading_notes") or []
        )
        or any(
            isinstance(item, dict) and item.get("set_id") == set_id
            for item in workspace.get("source_read_receipts") or []
        )
        or any(
            isinstance(item, dict) and item.get("set_id") == set_id
            for item in workspace.get("coverage_outcomes") or []
        )
    )
    return "resume" if has_durable_work else "first_pass"


def source_set_prompt_payload(workspace: dict[str, Any], set_id: str) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    source_set = _workspace_set(workspace, set_id) or {}
    questions_by_id = {
        str(item.get("question_id") or ""): item
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict)
    }
    capsules = workspace.get("source_capsules") or {}
    execution_mode = _source_set_execution_mode(workspace, source_set, set_id)
    targeted_repair = execution_mode == "targeted_repair"
    set_assessments = [
        item
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    set_findings = [
        item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    set_reading_notes = [
        item
        for item in workspace.get("reading_notes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    set_read_receipts = [
        item
        for item in workspace.get("source_read_receipts") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    latest_set_read_receipts = latest_source_read_receipts(set_read_receipts)
    read_coverage_ledger = source_read_coverage_ledger(set_read_receipts, set_id=set_id)
    pending_set_read_receipts = [
        item for item in set_read_receipts if not source_read_receipt_is_delivered(item)
    ]
    uncheckpointed_replays = uncheckpointed_source_read_replays(
        workspace,
        set_id=set_id,
    )
    pending_exact_refs = pending_exact_read_source_refs(workspace, source_set=source_set)
    pending_comparison_refs = pending_comparison_source_refs(source_set)
    stale_analysis_refs = source_refs_with_reads_newer_than_analysis(workspace, set_id=set_id)
    set_outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    assessed_refs = {
        str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "resolved") == "resolved"
        and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
    }
    outcomes_by_question = {
        str(item.get("question_id") or ""): item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    }
    completed_question_ids = {
        question_id
        for question_id, item in outcomes_by_question.items()
        if str(item.get("status") or "") in {"covered", "partial", "missing", "conflicted", "deferred"}
        and question_outcome_is_current(
            item,
            workspace=workspace,
            source_set=source_set,
            question_id=question_id,
        )
    }
    required_question_ids = [str(item) for item in source_set.get("question_ids") or []]
    requirement_assignments_by_question: dict[str, set[str]] = {}
    for assignment in source_set.get("question_assignments") or []:
        if not isinstance(assignment, dict):
            continue
        question_id = str(assignment.get("question_id") or "")
        if not question_id:
            continue
        requirement_assignments_by_question.setdefault(question_id, set()).update(
            str(value) for value in assignment.get("requirement_ids") or [] if str(value)
        )
    assigned_question_previews = [
        _compact_question_prompt_item(
            questions_by_id[question_id],
            requirement_ids=requirement_assignments_by_question.get(question_id),
        )
        for question_id in required_question_ids
        if question_id in questions_by_id
    ]
    other_question_index = [
        _compact_question_prompt_item(item, include_requirements=False)
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict) and str(item.get("question_id") or "") not in set(required_question_ids)
    ]
    capsule_previews = [
        _capsule_prompt_preview(capsule)
        for ref in source_set.get("source_refs") or []
        if isinstance((capsule := capsules.get(ref)), dict)
    ]
    if execution_mode == "semantic_eval":
        execution_goal = (
            "Resolve the assigned questions from frozen source analyses, findings, and evidence only. "
            "If the frozen material does not establish a criterion, use partial or missing; do not read sources "
            "or rewrite source analyses."
        )
    elif str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_ORIENTATION:
        execution_goal = (
            "Read every source independently, then link only genuine findings to existing substantive questions."
        )
    else:
        execution_goal = "Orient the assigned substantive coverage questions."
    return {
        "set": {
            "set_id": source_set.get("set_id"),
            "set_kind": source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE,
            "execution_goal": execution_goal,
            **_lean_text_fields("label", source_set.get("label") or "", 180),
            **_lean_text_fields("purpose", source_set.get("purpose") or "", 400),
            "source_refs": source_set.get("source_refs") or [],
            **_lean_text_fields("rationale", source_set.get("rationale") or "", 500),
            "assigned_questions": assigned_question_previews,
            "assigned_question_count": len(required_question_ids),
            "pending_source_refs": [
                str(item) for item in source_set.get("source_refs") or [] if str(item) not in assessed_refs
            ],
            "pending_exact_read_source_refs": pending_exact_refs,
            "pending_comparison_source_refs": pending_comparison_refs,
            "stale_source_analysis_refs": stale_analysis_refs,
            "pending_discovered_question_attempts": [
                {
                    "question_id": str(item.get("question_id") or ""),
                    "requirement_ids": [str(value) for value in item.get("requirement_ids") or []],
                    "finding_ids": [str(value) for value in item.get("finding_ids") or []],
                    "source_refs": [str(value) for value in item.get("source_refs") or []],
                    "reviewed_source_refs": [
                        str(value) for value in item.get("reviewed_source_refs") or []
                    ],
                }
                for item in source_set.get("pending_discovered_question_attempts") or []
                if isinstance(item, dict)
            ],
            "required_question_ids": required_question_ids,
            "missing_outcome_question_ids": [
                question_id for question_id in required_question_ids if question_id not in completed_question_ids
            ],
            "stale_outcome_question_ids": [
                question_id
                for question_id, item in outcomes_by_question.items()
                if not question_outcome_is_current(
                    item,
                    workspace=workspace,
                    source_set=source_set,
                    question_id=question_id,
                )
            ],
            "priority": source_set.get("priority"),
            "execution_mode": execution_mode,
            "repair_source_refs": (source_set.get("repair_source_refs") or []) if targeted_repair else [],
            "repair_question_ids": (source_set.get("repair_question_ids") or []) if targeted_repair else [],
            "repair_targets": [
                {
                    "code": item.get("code"),
                    **_lean_text_fields("message", item.get("message") or "", 400),
                    "source_refs": item.get("source_refs") or [],
                    "question_ids": item.get("question_ids") or [],
                }
                for item in source_set.get("repair_targets") or []
                if targeted_repair and isinstance(item, dict)
            ],
            "evidence_count": len(source_set.get("evidence_ids") or []),
            "source_assessment_count": len(set_assessments),
            "source_read_receipt_count": len(set_read_receipts),
            "reading_note_count": len(set_reading_notes),
            "source_finding_count": len(set_findings),
            "coverage_outcome_count": len(set_outcomes),
        },
        "capsule_previews": capsule_previews,
        "capsule_count": len(capsule_previews),
        "other_coverage_question_index": other_question_index,
        "other_coverage_question_count": len(other_question_index),
        "all_coverage_question_count": len(workspace.get("coverage_questions") or []),
        "saved_state_previews": {
            "source_read_coverage_ledger": read_coverage_ledger,
            "source_structure_coverage": [
                {
                    key: value
                    for key, value in structure.items()
                    if key != "items"
                }
                for source_ref in source_set.get("source_refs") or []
                if (
                    structure := source_structure_coverage(
                        workspace,
                        set_id=set_id,
                        source_ref=str(source_ref),
                    )
                )
            ],
            "latest_source_read_receipts": [
                {
                    "receipt_ref": item.get("receipt_ref"),
                    "source_ref": item.get("source_ref"),
                    "start_offset": item.get("start_offset"),
                    "end_offset": item.get("end_offset"),
                    "total_chars": item.get("total_chars"),
                    "read_kind": item.get("read_kind"),
                    "delivery_status": source_read_receipt_delivery_status(item),
                    "next_cursor": item.get("next_cursor") or "",
                    "eof": bool(item.get("eof")),
                }
                for item in latest_set_read_receipts
            ],
            "source_read_receipt_count": len(set_read_receipts),
            "source_read_receipt_preview_count": len(latest_set_read_receipts),
            "source_read_receipt_omitted_count": max(
                0, len(set_read_receipts) - len(latest_set_read_receipts)
            ),
            "pending_native_attachments": [
                {
                    "receipt_ref": item.get("receipt_ref"),
                    "source_ref": item.get("source_ref"),
                }
                for item in set_read_receipts
                if str(item.get("read_kind") or "") == "native_file_attachment"
                and str(item.get("delivery_status") or "pending") != "delivered"
            ],
            "pending_text_reads": [
                {
                    "receipt_ref": item.get("receipt_ref"),
                    "source_ref": item.get("source_ref"),
                    "read_kind": item.get("read_kind"),
                    "start_offset": item.get("start_offset"),
                    "end_offset": item.get("end_offset"),
                    "retry": (
                        {
                            "tool": "read_source",
                            "source_ref": item.get("source_ref"),
                            "cursor": next(
                                (
                                    str(delivered.get("receipt_ref") or "")
                                    for delivered in reversed(latest_set_read_receipts)
                                    if str(delivered.get("source_ref") or "")
                                    == str(item.get("source_ref") or "")
                                    and str(delivered.get("read_kind") or "") == "sequential"
                                ),
                                "",
                            ),
                            "max_chars": (item.get("request") or {}).get("max_chars"),
                        }
                        if str(item.get("read_kind") or "") == "sequential"
                        else {
                            "tool": "search_source",
                            "source_ref": item.get("source_ref"),
                            "query": (item.get("request") or {}).get("query"),
                            "max_results": (item.get("request") or {}).get("max_results"),
                            "max_chars": (item.get("request") or {}).get("max_chars"),
                        }
                    ),
                }
                for item in pending_set_read_receipts
                if str(item.get("read_kind") or "") in {"sequential", "search"}
            ],
            "replay_required_after_activation": uncheckpointed_replays,
            "reading_notes": [
                {
                    "note_id": item.get("note_id"),
                    "source_ref": item.get("source_ref"),
                    "receipt_refs": item.get("receipt_refs") or [],
                    **_lean_text_fields(
                        "what_was_read",
                        item.get("what_was_read") or "",
                        320,
                    ),
                    **_lean_text_fields(
                        "what_was_established",
                        item.get("what_was_established") or "",
                        420,
                    ),
                    **_lean_text_fields(
                        "unread_regions",
                        item.get("unread_regions") or "",
                        260,
                    ),
                    **_lean_text_fields(
                        "unread_risk_assessment",
                        item.get("unread_risk_assessment")
                        or item.get("why_unread_is_safe")
                        or "",
                        260,
                    ),
                    "question_ids": [
                        link.get("question_id")
                        for link in item.get("question_links") or []
                        if isinstance(link, dict)
                    ],
                    "doubt_count": len(item.get("doubts") or []),
                    "next_check_count": len(item.get("next_checks") or []),
                }
                for item in set_reading_notes[:12]
            ],
            "reading_note_preview_count": min(len(set_reading_notes), 12),
            "reading_note_omitted_count": max(0, len(set_reading_notes) - 12),
            "source_analyses": [
                {
                    "source_ref": item.get("source_ref"),
                    "representation_status": item.get("representation_status"),
                    "disposition": item.get("disposition"),
                    "finding_ids": item.get("finding_ids") or [],
                    **_lean_text_fields(
                        "no_findings_reason",
                        item.get("no_findings_reason") or "",
                        260,
                    ),
                }
                for item in set_assessments
            ],
            "findings": [
                {
                    "finding_id": item.get("finding_id"),
                    "source_ref": item.get("source_ref"),
                    **_lean_text_fields("statement", item.get("statement") or "", 320),
                    "question_links": [
                        {
                            "question_id": link.get("question_id"),
                            "requirement_ids": link.get("requirement_ids") or [],
                            **_lean_text_fields("rationale", link.get("rationale") or "", 180),
                        }
                        for link in item.get("question_links") or []
                        if isinstance(link, dict)
                    ],
                    "evidence_count": len(item.get("evidence_ids") or []),
                    "semantic_revision": item.get("semantic_revision"),
                }
                for item in set_findings[:20]
            ],
            "finding_preview_count": min(len(set_findings), 20),
            "finding_omitted_count": max(0, len(set_findings) - 20),
            "question_outcomes": [
                {
                    "question_id": item.get("question_id"),
                    "status": item.get("status"),
                    "is_stale": bool(item.get("is_stale")),
                    "finding_ids": item.get("finding_ids") or [],
                    "requirement_statuses": [
                        {
                            "requirement_id": result.get("requirement_id"),
                            "status": result.get("status"),
                        }
                        for result in item.get("requirement_results") or []
                        if isinstance(result, dict)
                    ],
                    **_lean_text_fields(
                        "orientation_summary",
                        item.get("orientation_summary") or "",
                        320,
                    ),
                }
                for item in set_outcomes
            ],
            "full_records": (
                "Use set_overview (current_set_state compatibility alias) and unfinished_work first; then page "
                "source_memory for one source, "
                "or every question_workbench page for one question before resolving it."
            ),
        },
        "workspace_progress": {
            "status": workspace.get("status"),
            "source_count": len(workspace.get("source_order") or []),
            "source_set_count": len(workspace.get("source_sets") or []),
            "processed_set_count": len(workspace.get("processed_sets") or []),
            "pending_set_count": len(unresolved_source_sets(workspace)),
            "coverage_question_count": len(workspace.get("coverage_questions") or []),
            "source_assessment_count": len(workspace.get("source_assessments") or []),
            "source_read_receipt_count": len(workspace.get("source_read_receipts") or []),
            "reading_note_count": len(workspace.get("reading_notes") or []),
            "source_finding_count": len(workspace.get("source_findings") or []),
            "coverage_outcome_count": len(workspace.get("coverage_outcomes") or []),
        },
    }


def _compact_question_prompt_item(
    item: dict[str, Any],
    *,
    requirement_ids: set[str] | None = None,
    include_requirements: bool = True,
) -> dict[str, Any]:
    requirements = [
        requirement
        for requirement in item.get("answer_requirements") or []
        if isinstance(requirement, dict)
        and (
            requirement_ids is None
            or str(requirement.get("requirement_id") or "") in requirement_ids
        )
    ]
    payload: dict[str, Any] = {
        "question_id": item.get("question_id"),
        **_lean_text_fields("question", item.get("question") or "", 280),
        "kind": item.get("kind"),
        "importance": item.get("importance"),
        "parent_question_id": item.get("parent_question_id"),
    }
    if not include_requirements:
        payload["requirement_count"] = len(item.get("answer_requirements") or [])
        return payload
    return {
        **payload,
        "answer_requirements": [
            {
                "requirement_id": requirement.get("requirement_id"),
                **_lean_text_fields(
                    "description",
                    requirement.get("description") or "",
                    240,
                ),
                **_lean_text_fields("rationale", requirement.get("rationale") or "", 220),
                **_lean_text_fields(
                    "completion_criterion",
                    requirement.get("completion_criterion") or "",
                    240,
                ),
            }
            for requirement in requirements
        ],
        "assigned_requirement_count": len(requirements),
    }


def unprocessed_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "in_progress", "needs_repair"}
        or _source_set_has_unresolved_refs(workspace, item)
    ]


__all__ = [
    "checkpoint_workspace_payload",
    "planner_resume_payload",
    "planner_source_catalog",
    "source_set_prompt_payload",
    "unprocessed_source_sets",
    "unresolved_source_set_summary",
    "unresolved_source_sets",
    "workspace_summary_for_prompt",
]
