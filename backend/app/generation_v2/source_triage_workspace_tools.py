from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as _workspace
from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.model_facing_record_parts import composite_record_parts
from app.generation_v2.runtime import AgentTerminalResult, AgentTool
from app.generation_v2.schemas import SourceTriageCompletion
from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_lineage import coverage_synthesis_lineage
from app.generation_v2.source_triage_model_views import (
    model_safe_coverage_hint as _model_safe_coverage_hint,
    model_safe_coverage_outcome as _model_safe_coverage_outcome,
    model_safe_coverage_question as _model_safe_coverage_question,
    model_safe_reading_note as _model_safe_reading_note,
    model_safe_source_assessment as _model_safe_source_assessment,
    model_safe_source_finding as _model_safe_source_finding,
    model_safe_source_read_receipt as _model_safe_source_read_receipt,
    reading_note_record_parts as _reading_note_record_parts,
)
from app.generation_v2.source_triage_set_review import (
    semantic_review_evidence_ids,
    semantic_review_page,
    semantic_self_check_page,
)
from app.generation_v2.source_triage_model_delivery import (
    finding_catalog_receipt_ref,
    fit_staged_model_page,
    question_workbench_receipt_ref,
    semantic_review_receipt_ref,
    stage_finding_catalog_page,
    stage_question_workbench_page,
    stage_semantic_review_page,
)
from app.generation_v2.source_triage_set_state_views import (
    coverage_hints_page,
    finding_catalog_page,
    question_index_page,
    question_memory_page,
    question_workbench_page,
    set_overview_page,
    source_memory_page,
    unfinished_work_page,
)
from app.generation_v2.tools.evidence_legacy_receipts import hydrate_legacy_source_receipts
from app.generation_v2.source_triage_workspace_tool_handlers import SourceTriageWorkspaceToolHandlers
from app.generation_v2.source_triage_workspace_tool_specs import (
    source_mapper_tools,
    source_triage_finalizer_tools,
    source_triage_planner_tools,
    source_triage_set_tools,
)
from app.models.generation_run import GenerationRun


TRIAGE_WORKSPACE_DEFAULT_PAGE_LIMIT = 20
TRIAGE_WORKSPACE_MAX_PAGE_LIMIT = 25
FINALIZER_REVIEW_SECTIONS = ("coverage_questions", "source_findings", "coverage_hints")
FINALIZER_READ_LEDGER_FIELD = "source_triage_finalizer_reads"


def _checked_page_output(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded) > MODEL_TOOL_OUTPUT_MAX_CHARS:
        return {
            "accepted": False,
            "error": "triage_workspace_item_exceeds_model_read_contract",
            "max_chars": MODEL_TOOL_OUTPUT_MAX_CHARS,
        }
    return payload


def _page_metadata(*, total: int, cursor: int, returned: int) -> dict[str, Any]:
    next_offset = cursor + returned
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - next_offset),
        "cursor": str(cursor),
        "next_cursor": str(next_offset) if next_offset < total else "",
    }


def _full_source_set_item(source_set: dict[str, Any]) -> dict[str, Any]:
    question_ids = [str(item) for item in source_set.get("question_ids") or []]
    legacy_questions = list(source_set.get("questions") or [])
    return {
        "set_id": source_set.get("set_id"),
        "set_kind": source_set.get("set_kind") or "coverage",
        "label": str(source_set.get("label") or ""),
        "purpose": str(source_set.get("purpose") or ""),
        "source_refs": source_set.get("source_refs") or [],
        "question_ids": question_ids,
        "question_count": max(len(question_ids), len(legacy_questions)),
        "legacy_question_count": len(legacy_questions),
        "rationale": str(source_set.get("rationale") or ""),
        "priority": source_set.get("priority"),
        "status": source_set.get("status"),
        "findings_summary": str(source_set.get("findings_summary") or ""),
        "deferred_reason": str(source_set.get("deferred_reason") or ""),
        "evidence_ids": [str(item) for item in source_set.get("evidence_ids") or []],
        "observation_count": len(source_set.get("observations") or []),
        "written_action_count": len(source_set.get("written_actions") or []),
    }


def _full_triage_observation_item(
    observation: dict[str, Any],
    *,
    observation_number: int,
    include_evidence_ids: bool,
) -> dict[str, Any]:
    evidence_ids = [str(item) for item in observation.get("evidence_ids") or [] if str(item).strip()]
    return {
        "observation_number": observation_number,
        "source_refs": observation.get("source_refs") or [],
        **({"evidence_ids": evidence_ids} if include_evidence_ids and evidence_ids else {}),
        "evidence_count": len(evidence_ids),
        "evidence_omitted_count": 0 if include_evidence_ids else len(evidence_ids),
        "note": str(observation.get("note") or ""),
        "pending_questions": [str(item) for item in observation.get("pending_questions") or []],
        "created_at": observation.get("created_at"),
    }


def _full_plan_summary_page(workspace: dict[str, Any], *, cursor: int) -> dict[str, Any]:
    summary = str(workspace.get("plan_summary") or "")
    items = [{"summary": summary}] if cursor == 0 and summary else []
    return {
        "accepted": True,
        "section": "plan_summary",
        "items": items,
        "page": _page_metadata(total=1 if summary else 0, cursor=cursor, returned=len(items)),
    }


def _full_deferred_source_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    deferred = [item for item in workspace.get("deferred_sources") or [] if isinstance(item, dict)]
    page = deferred[cursor : cursor + limit]
    items = [
        {
            "source_ref": item.get("source_ref"),
            "reason": str(item.get("reason") or ""),
        }
        for item in page
    ]
    return fit_model_page(
        {
            "accepted": True,
            "section": "deferred_sources",
            "items": items,
            "page": _page_metadata(total=len(deferred), cursor=cursor, returned=len(items)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _full_source_map_repair_page(workspace: dict[str, Any], *, cursor: int, limit: int) -> dict[str, Any]:
    repairs = [item for item in workspace.get("source_map_repair_items") or [] if isinstance(item, dict)]
    page = repairs[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "source_map_repairs",
            "items": page,
            "page": _page_metadata(total=len(repairs), cursor=cursor, returned=len(page)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _full_canonical_item_page(
    workspace: dict[str, Any],
    *,
    section: str,
    cursor: int,
    limit: int = 1,
    current_set_id: str | None = None,
) -> dict[str, Any]:
    values, parts = _canonical_section_record_parts(
        workspace,
        section=section,
        current_set_id=current_set_id,
    )
    page = parts[cursor : cursor + limit]
    payload = {
        "accepted": True,
        "section": section,
        "record_count": len(values),
        "items": page,
        "page": {
            **_page_metadata(total=len(parts), cursor=cursor, returned=len(page)),
            "unit": "record_part",
        }
    }
    return fit_model_page(
        payload,
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )

def _canonical_section_record_parts(
    workspace: dict[str, Any],
    *,
    section: str,
    current_set_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash

    field_by_section = {
        "coverage_questions": "coverage_questions",
        "source_read_receipts": "source_read_receipts",
        "reading_notes": "reading_notes",
        "source_assessments": "source_assessments",
        "source_findings": "source_findings",
        "coverage_hints": "coverage_hints",
        "coverage_outcomes": "coverage_outcomes",
        "coverage_syntheses": "coverage_syntheses",
    }
    field = field_by_section[section]
    values = [item for item in workspace.get(field) or [] if isinstance(item, dict)]
    if current_set_id and section in {
        "source_read_receipts",
        "reading_notes",
        "source_assessments",
        "source_findings",
        "coverage_hints",
        "coverage_outcomes",
    }:
        values = [item for item in values if str(item.get("set_id") or "") == current_set_id]
        baseline_field = {
            "source_read_receipts": "",
            "reading_notes": "",
            "source_assessments": "repair_baseline_source_assessments",
            "source_findings": "repair_baseline_source_findings",
            "coverage_hints": "repair_baseline_coverage_hints",
            "coverage_outcomes": "repair_baseline_coverage_outcomes",
        }[section]
        baselines = [
            item
            for item in (workspace.get(baseline_field) or []) if baseline_field
            if isinstance(item, dict) and str(item.get("set_id") or "") == current_set_id
        ]
        if section == "source_assessments":
            current_keys = {
                str(item.get("source_ref") or "")
                for item in values
                if str(item.get("processing_status") or "resolved") == "resolved"
            }
            values.extend(
                item for item in baselines if str(item.get("source_ref") or "") not in current_keys
            )
        elif section == "coverage_outcomes":
            current_keys = {
                (str(item.get("set_id") or ""), str(item.get("question_id") or ""))
                for item in values
                if str(item.get("status") or "")
                in {"covered", "partial", "missing", "conflicted", "deferred"}
            }
            values.extend(
                item
                for item in baselines
                if (str(item.get("set_id") or ""), str(item.get("question_id") or "")) not in current_keys
            )
        elif section in {"source_findings", "coverage_hints"}:
            values.extend(baselines)
    values.sort(key=lambda item: _canonical_item_identity(section, item))
    source_ref_by_id = {
        str(item.get("source_id") or ""): str(item.get("source_ref") or "")
        for item in [
            *(workspace.get("source_assessments") or []),
            *(workspace.get("repair_baseline_source_assessments") or []),
        ]
        if isinstance(item, dict) and item.get("source_id") and item.get("source_ref")
    }
    serializers = {
        "coverage_questions": _model_safe_coverage_question,
        "source_read_receipts": _model_safe_source_read_receipt,
        "reading_notes": _model_safe_reading_note,
        "source_assessments": _model_safe_source_assessment,
        "source_findings": _model_safe_source_finding,
        "coverage_hints": _model_safe_coverage_hint,
        "coverage_outcomes": lambda item: _model_safe_coverage_outcome(
            item,
            source_ref_by_id=source_ref_by_id,
        ),
        "coverage_syntheses": lambda item: _model_safe_coverage_synthesis(
            item,
            source_ref_by_id=source_ref_by_id,
            current_revision=int(workspace.get("triage_semantic_revision") or 0),
            current_input_hash=coverage_synthesis_input_hash(
                workspace,
                str(item.get("question_id") or ""),
            ),
        ),
    }
    serialized = [serializers[section](item) for item in values]
    record_kind_by_section = {
        "coverage_questions": "coverage_question",
        "source_read_receipts": "source_read_receipt",
        "reading_notes": "reading_note",
        "source_assessments": "source_assessment",
        "source_findings": "source_finding",
        "coverage_hints": "coverage_hint",
        "coverage_outcomes": "coverage_outcome",
        "coverage_syntheses": "coverage_synthesis",
    }
    parts = [
        part
        for item in serialized
        for part in (
            _reading_note_record_parts(item)
            if section == "reading_notes"
            else composite_record_parts(
                item,
                record_kind=record_kind_by_section[section],
                owner_ref=(
                    str(item.get("receipt_ref") or "source_read_receipt:unknown")
                    if section == "source_read_receipts"
                    else None
                ),
            )
        )
    ]
    return values, parts


def _canonical_item_identity(section: str, item: dict[str, Any]) -> tuple[str, ...]:
    """Keep offset pages stable when an upsert moves an existing record to the list tail."""

    if section == "coverage_questions":
        return (str(item.get("question_id") or ""),)
    if section == "source_read_receipts":
        return (str(item.get("receipt_ref") or ""),)
    if section == "reading_notes":
        return (str(item.get("note_id") or ""),)
    if section == "source_assessments":
        return (
            str(item.get("source_ref") or ""),
            str(item.get("set_id") or ""),
            "1" if item.get("repair_baseline") else "0",
        )
    if section == "source_findings":
        return (
            str(item.get("finding_id") or ""),
            "1" if item.get("repair_baseline") else "0",
        )
    if section == "coverage_hints":
        return (
            str(item.get("hint_id") or ""),
            "1" if item.get("repair_baseline") else "0",
        )
    if section == "coverage_outcomes":
        return (
            str(item.get("question_id") or ""),
            str(item.get("set_id") or ""),
            "1" if item.get("repair_baseline") else "0",
        )
    if section == "coverage_syntheses":
        return (str(item.get("question_id") or ""),)
    return (json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),)


def _record_part_review_key(part: dict[str, Any]) -> str:
    return sha256(
        json.dumps(part, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _record_finalizer_review_page(
    workspace: dict[str, Any],
    *,
    section: str,
    page_payload: dict[str, Any],
) -> None:
    if section not in FINALIZER_REVIEW_SECTIONS or page_payload.get("accepted") is not True:
        return
    _, expected_parts = _canonical_section_record_parts(workspace, section=section)
    expected_keys = {_record_part_review_key(part) for part in expected_parts}
    returned_keys = {
        _record_part_review_key(part)
        for part in page_payload.get("items") or []
        if isinstance(part, dict)
    }
    ledger = dict(workspace.get(FINALIZER_READ_LEDGER_FIELD) or {})
    previous = ledger.get(section) if isinstance(ledger.get(section), dict) else {}
    read_keys = {
        str(value)
        for value in previous.get("read_part_keys") or []
        if str(value) in expected_keys
    }
    read_keys.update(returned_keys & expected_keys)
    ledger[section] = {
        "read_part_keys": sorted(read_keys),
        "read_part_count": len(read_keys),
        "expected_part_count": len(expected_keys),
        "complete": expected_keys.issubset(read_keys),
        "updated_at": _workspace._now(),
    }
    workspace[FINALIZER_READ_LEDGER_FIELD] = ledger


def finalizer_review_stream_violations(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    """Legacy compatibility hook.

    Source-mapping finalization no longer requires a full Q/F/H paging pass. The finalizer may read paged state
    when it needs a concrete record, while backend readiness is enforced by canonical reference validation.
    """
    return []


def carry_finalizer_finding_review_forward(
    workspace: dict[str, Any],
    *,
    previous_finding: dict[str, Any],
) -> bool:
    """Carry review credit through an atomic finding rewrite or removal."""

    section = "source_findings"
    ledger = dict(workspace.get(FINALIZER_READ_LEDGER_FIELD) or {})
    section_ledger = ledger.get(section) if isinstance(ledger.get(section), dict) else {}
    read_keys = {str(value) for value in section_ledger.get("read_part_keys") or []}
    previous_parts = composite_record_parts(
        _model_safe_source_finding(previous_finding),
        record_kind="source_finding",
    )
    previous_keys = {_record_part_review_key(part) for part in previous_parts}
    if not previous_keys or not previous_keys.issubset(read_keys):
        return False

    finding_id = str(previous_finding.get("finding_id") or "")
    current_finding = next(
        (
            item
            for item in workspace.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("finding_id") or "") == finding_id
        ),
        None,
    )
    current_parts = (
        composite_record_parts(
            _model_safe_source_finding(current_finding),
            record_kind="source_finding",
        )
        if current_finding is not None
        else []
    )
    current_keys = {_record_part_review_key(part) for part in current_parts}
    _, expected_parts = _canonical_section_record_parts(workspace, section=section)
    expected_keys = {_record_part_review_key(part) for part in expected_parts}
    next_read_keys = ((read_keys - previous_keys) | current_keys) & expected_keys
    ledger[section] = {
        "read_part_keys": sorted(next_read_keys),
        "read_part_count": len(next_read_keys),
        "expected_part_count": len(expected_keys),
        "complete": expected_keys.issubset(next_read_keys),
        "updated_at": _workspace._now(),
    }
    workspace[FINALIZER_READ_LEDGER_FIELD] = ledger
    return True


def _full_assigned_question_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    questions_by_id = {
        str(item.get("question_id") or ""): item
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict)
    }
    question_ids = [str(item) for item in source_set.get("question_ids") or []]
    values = [questions_by_id[question_id] for question_id in question_ids if question_id in questions_by_id]
    included_ids = {str(item.get("question_id") or "") for item in values}
    for index, raw in enumerate(source_set.get("questions") or []):
        question_id = question_ids[index] if index < len(question_ids) else f"legacy_question_{index + 1}"
        if question_id in included_ids:
            continue
        if isinstance(raw, dict):
            values.append(
                {
                    "question_id": str(raw.get("question_id") or question_id),
                    "question": str(raw.get("question") or raw.get("text") or ""),
                    "kind": raw.get("kind"),
                    "importance": raw.get("importance"),
                    "rationale": str(raw.get("rationale") or ""),
                    "parent_question_id": raw.get("parent_question_id"),
                    "origin": raw.get("origin") or "legacy_source_set",
                }
            )
        else:
            values.append(
                {
                    "question_id": question_id,
                    "question": str(raw),
                    "kind": None,
                    "importance": None,
                    "rationale": "",
                    "parent_question_id": None,
                    "origin": "legacy_source_set",
                }
            )
        included_ids.add(question_id)
    parts = [
        part
        for item in values
        for part in composite_record_parts(
            _model_safe_coverage_question(item),
            record_kind="coverage_question",
        )
    ]
    page = parts[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "assigned_questions",
            "set_id": source_set.get("set_id"),
            "record_count": len(values),
            "items": page,
            "page": {
                **_page_metadata(total=len(parts), cursor=cursor, returned=len(page)),
                "unit": "record_part",
            },
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _current_set_state_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    """Compatibility view: compact overview only, never the former monolithic state stream."""

    return set_overview_page(
        workspace,
        source_set,
        cursor=cursor,
        limit=limit,
        section="current_set_state",
    )


def _model_safe_coverage_synthesis(
    item: dict[str, Any],
    *,
    source_ref_by_id: dict[str, str],
    current_revision: int,
    current_input_hash: str,
) -> dict[str, Any]:
    payload = _model_safe_coverage_outcome(
        item,
        source_ref_by_id=source_ref_by_id,
        include_next_action=True,
    )
    payload.pop("set_id", None)
    payload["synthesis_ref"] = str(item.get("outcome_id") or "")
    payload["input_current"] = bool(
        int(item.get("input_revision") or 0) == current_revision
        and str(item.get("input_hash") or "") == current_input_hash
    )
    payload.pop("outcome_ref", None)
    return payload


def _coverage_question_packet_page(
    workspace: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash

    source_ref_by_id = {
        str(item.get("source_id") or ""): str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict) and item.get("source_id") and item.get("source_ref")
    }
    questions = sorted(
        [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)],
        key=lambda item: _canonical_item_identity("coverage_questions", item),
    )
    outcomes = sorted(
        [item for item in workspace.get("coverage_outcomes") or [] if isinstance(item, dict)],
        key=lambda item: _canonical_item_identity("coverage_outcomes", item),
    )
    question_ids = {str(item.get("question_id") or "") for item in questions}
    overlap_parts_by_question = _cross_question_overlap_parts(
        [item for item in outcomes if str(item.get("question_id") or "") in question_ids],
        source_ref_by_id=source_ref_by_id,
    )
    syntheses = {
        str(item.get("question_id") or ""): item
        for item in workspace.get("coverage_syntheses") or []
        if isinstance(item, dict) and str(item.get("question_id") or "").strip()
    }
    packet_parts: list[dict[str, Any]] = []
    for question in questions:
        question_id = str(question.get("question_id") or "")
        packet_ref = f"coverage_question:{question_id}"
        question_outcomes = [
            item for item in outcomes if str(item.get("question_id") or "") == question_id
        ]
        active_sources, active_evidence, active_outcome_refs = coverage_synthesis_lineage(
            question_outcomes,
            status="covered",
            source_ref_by_id=source_ref_by_id,
        )
        closed_sources, closed_evidence, closed_outcome_refs = coverage_synthesis_lineage(
            question_outcomes,
            status="missing",
            source_ref_by_id=source_ref_by_id,
        )
        existing = syntheses.get(question_id)
        packet_parts.extend(
            _packet_composite_record_parts(
                packet_ref=packet_ref,
                packet_part_kind="question",
                record=_model_safe_coverage_question(question),
                record_kind="coverage_question",
                packet_metadata={
                    "per_set_outcome_count": len(question_outcomes),
                    "derived_lineage": {
                        "backend_derived": True,
                        "for_covered_partial_or_conflicted": {
                            "outcome_count": len(active_outcome_refs),
                            "source_count": len(active_sources),
                            "evidence_count": len(active_evidence),
                        },
                        "for_missing_or_deferred": {
                            "outcome_count": len(closed_outcome_refs),
                            "source_count": len(closed_sources),
                            "evidence_count": len(closed_evidence),
                        },
                    },
                },
            )
        )
        existing_synthesis = (
            _model_safe_coverage_synthesis(
                existing,
                source_ref_by_id=source_ref_by_id,
                current_revision=int(workspace.get("triage_semantic_revision") or 0),
                current_input_hash=coverage_synthesis_input_hash(workspace, question_id),
            )
            if existing is not None
            else None
        )
        packet_parts.extend(
            _packet_record_parts(
                packet_ref=packet_ref,
                part_kind="existing_synthesis",
                record=existing_synthesis,
            )
        )
        for outcome in question_outcomes:
            packet_parts.extend(
                _packet_record_parts(
                    packet_ref=packet_ref,
                    part_kind="per_set_outcome",
                    record=_model_safe_coverage_outcome(
                        outcome,
                        source_ref_by_id=source_ref_by_id,
                    ),
                )
            )
        packet_parts.extend(overlap_parts_by_question.get(question_id, []))
    page_parts = packet_parts[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "coverage_question_packets",
            "packet_contract": "ordered_parts_v1",
            "question_count": len(questions),
            "items": page_parts,
            "page": {
                **_page_metadata(total=len(packet_parts), cursor=cursor, returned=len(page_parts)),
                "unit": "packet_part",
            },
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _cross_question_overlap_parts(
    outcomes: list[dict[str, Any]],
    *,
    source_ref_by_id: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Expose factual lineage intersections without deciding what they mean."""

    model_outcomes = [
        _model_safe_coverage_outcome(item, source_ref_by_id=source_ref_by_id)
        for item in outcomes
    ]
    model_outcomes.sort(
        key=lambda item: (
            str(item.get("question_id") or ""),
            str(item.get("set_id") or ""),
            str(item.get("outcome_ref") or ""),
        )
    )
    question_id_by_outcome_ref: dict[str, str] = {}
    memberships: dict[tuple[str, str], dict[str, None]] = {}
    for outcome in model_outcomes:
        question_id = str(outcome.get("question_id") or "")
        outcome_ref = str(outcome.get("outcome_ref") or "")
        if not question_id or not outcome_ref:
            continue
        question_id_by_outcome_ref[outcome_ref] = question_id
        factual_keys = [
            *(("shared_set_ids", str(outcome.get("set_id") or "")),),
            *(
                ("source_refs", str(value))
                for value in outcome.get("source_refs") or []
                if str(value).strip()
            ),
            *(
                ("evidence_ids", str(value))
                for value in outcome.get("evidence_ids") or []
                if str(value).strip()
            ),
        ]
        for field, value in factual_keys:
            if value:
                memberships.setdefault((field, value), {})[outcome_ref] = None

    grouped_keys: dict[tuple[str, ...], dict[str, list[str]]] = {}
    for (field, value), members in sorted(memberships.items()):
        outcome_refs = tuple(members)
        question_ids = {question_id_by_outcome_ref[ref] for ref in outcome_refs}
        if len(question_ids) < 2:
            continue
        grouped_keys.setdefault(
            outcome_refs,
            {"shared_set_ids": [], "source_refs": [], "evidence_ids": []},
        )[field].append(value)

    parts_by_question: dict[str, list[dict[str, Any]]] = {}
    for outcome_refs, shared_keys in sorted(grouped_keys.items()):
        question_ids = list(
            dict.fromkeys(question_id_by_outcome_ref[outcome_ref] for outcome_ref in outcome_refs)
        )
        question_refs = [f"coverage_question:{question_id}" for question_id in question_ids]
        overlap_digest = sha256("\x1f".join(outcome_refs).encode()).hexdigest()
        record = {
            "overlap_ref": f"cross_overlap:{overlap_digest}",
            "question_refs": question_refs,
            "outcome_refs": list(outcome_refs),
            **shared_keys,
        }
        anchor_question_id = question_ids[-1]
        # Anchor after the latest referenced question so all outcome records have
        # already appeared. Identical memberships share one compact record.
        parts_by_question.setdefault(anchor_question_id, []).extend(
            _packet_record_parts(
                packet_ref=f"coverage_question:{anchor_question_id}",
                part_kind="cross_question_overlap",
                record=record,
                lineage_fields=(
                    ("question_refs", "question_ref_count"),
                    ("outcome_refs", "outcome_ref_count"),
                    ("shared_set_ids", "shared_set_id_count"),
                    ("source_refs", "source_ref_count"),
                    ("evidence_ids", "evidence_id_count"),
                ),
            )
        )
    return parts_by_question


def _packet_record_parts(
    *,
    packet_ref: str,
    part_kind: str,
    record: dict[str, Any] | None,
    lineage_fields: tuple[tuple[str, str], ...] = (
        ("source_refs", "source_ref_count"),
        ("evidence_ids", "evidence_id_count"),
    ),
) -> list[dict[str, Any]]:
    owner_ref = _packet_record_owner_ref(
        packet_ref=packet_ref,
        part_kind=part_kind,
        record=record,
    )
    full_part = {
        "packet_ref": packet_ref,
        "part_kind": part_kind,
        "owner_ref": owner_ref,
        part_kind: record,
    }
    if record is None or _packet_part_fits(full_part):
        return [full_part]

    if part_kind in {"existing_synthesis", "per_set_outcome"}:
        semantic_record = dict(record)
        lineage_values = {
            field: [str(value) for value in semantic_record.pop(field, []) or []]
            for field, _count_field in lineage_fields
        }
        parts = _packet_composite_record_parts(
            packet_ref=packet_ref,
            packet_part_kind=part_kind,
            record=semantic_record,
            record_kind=(
                "coverage_synthesis" if part_kind == "existing_synthesis" else "coverage_outcome"
            ),
            owner_ref=owner_ref,
            packet_metadata={
                count_field: len(lineage_values[field])
                for field, count_field in lineage_fields
            },
        )
        if any(lineage_values.values()):
            parts.extend(
                _packet_lineage_parts(
                    packet_ref=packet_ref,
                    owner_kind=part_kind,
                    owner_ref=owner_ref,
                    lineage_values=lineage_values,
                )
            )
        return parts

    lineage_values = {
        field: [str(value) for value in record.get(field) or []]
        for field, _count_field in lineage_fields
    }
    semantic_record = dict(record)
    for field, _count_field in lineage_fields:
        semantic_record.pop(field, None)
    semantic_part = {
        "packet_ref": packet_ref,
        "part_kind": part_kind,
        "owner_ref": owner_ref,
        part_kind: semantic_record,
        **{
            count_field: len(lineage_values[field])
            for field, count_field in lineage_fields
        },
        "lineage_continues": any(lineage_values.values()),
    }
    parts = [semantic_part]
    if any(lineage_values.values()):
        parts.extend(
            _packet_lineage_parts(
                packet_ref=packet_ref,
                owner_kind=part_kind,
                owner_ref=owner_ref,
                lineage_values=lineage_values,
            )
        )
    return parts


def _packet_composite_record_parts(
    *,
    packet_ref: str,
    packet_part_kind: str,
    record: dict[str, Any],
    record_kind: str,
    owner_ref: str | None = None,
    packet_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    metadata = dict(packet_metadata or {})
    return [
        {
            "packet_ref": packet_ref,
            "part_kind": packet_part_kind,
            "owner_ref": part.get("owner_ref"),
            "record_part_kind": part.get("part_kind"),
            packet_part_kind: part,
            **metadata,
        }
        for part in composite_record_parts(
            record,
            record_kind=record_kind,
            owner_ref=owner_ref,
        )
    ]


def _packet_record_owner_ref(
    *,
    packet_ref: str,
    part_kind: str,
    record: dict[str, Any] | None,
) -> str:
    if record is not None:
        for field in (
            "overlap_ref",
            "outcome_ref",
            "synthesis_ref",
            "finding_id",
            "question_id",
        ):
            value = str(record.get(field) or "")
            if value:
                return value
    return f"{packet_ref}:{part_kind}"


def _packet_lineage_parts(
    *,
    packet_ref: str,
    owner_kind: str,
    owner_ref: str,
    lineage_values: dict[str, list[str]],
) -> list[dict[str, Any]]:
    entries = [
        (field, value)
        for field, values in lineage_values.items()
        for value in values
    ]
    total = len(entries)
    parts: list[dict[str, Any]] = []
    offset = 0
    while offset < total:
        returned = 0
        best: dict[str, Any] | None = None
        while offset + returned < total:
            candidate_entries = entries[offset : offset + returned + 1]
            candidate = _packet_lineage_part(
                packet_ref=packet_ref,
                owner_kind=owner_kind,
                owner_ref=owner_ref,
                entries=candidate_entries,
                fields=tuple(lineage_values),
                cursor=offset,
                total=total,
            )
            if not _packet_part_fits(candidate):
                break
            best = candidate
            returned += 1
        if best is None:
            # Public source refs and evidence ids are bounded identifiers. Reaching this means
            # persisted state violates that contract; keep the failure explicit rather than cut it.
            best = _packet_lineage_part(
                packet_ref=packet_ref,
                owner_kind=owner_kind,
                owner_ref=owner_ref,
                entries=entries[offset : offset + 1],
                fields=tuple(lineage_values),
                cursor=offset,
                total=total,
            )
            returned = 1
        parts.append(best)
        offset += returned
    return parts


def _packet_lineage_part(
    *,
    packet_ref: str,
    owner_kind: str,
    owner_ref: str,
    entries: list[tuple[str, str]],
    fields: tuple[str, ...],
    cursor: int,
    total: int,
) -> dict[str, Any]:
    returned = len(entries)
    return {
        "packet_ref": packet_ref,
        "part_kind": "lineage",
        "owner_kind": owner_kind,
        "owner_ref": owner_ref,
        **{
            field: [value for entry_field, value in entries if entry_field == field]
            for field in fields
        },
        "lineage_page": {
            "total_count": total,
            "returned_count": returned,
            "omitted_count": max(0, total - cursor - returned),
            "cursor": str(cursor),
            "next_cursor": str(cursor + returned) if cursor + returned < total else "",
        },
    }


def _packet_part_fits(part: dict[str, Any]) -> bool:
    fitted = fit_model_page(
        {
            "accepted": True,
            "section": "coverage_question_packets",
            "packet_contract": "ordered_parts_v1",
            "question_count": 1,
            "items": [part],
            "page": {
                **_page_metadata(total=1, cursor=0, returned=1),
                "unit": "packet_part",
            },
        },
        requested_limit=TRIAGE_WORKSPACE_MAX_PAGE_LIMIT,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )
    return bool(
        fitted.get("accepted") is True
        and int((fitted.get("page") or {}).get("returned_count") or 0) == 1
    )


def _source_set_page_tool(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    current_set_id: str | None = None,
    include_evidence_ids: bool = False,
    finalizer_mode: bool = False,
    track_finalizer_review: bool = False,
    evidence_toolset: Any | None = None,
) -> AgentTool:
    configured_set = (
        next(
            (
                item
                for item in workspace.get("source_sets") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") == str(current_set_id or "")
            ),
            None,
        )
        if current_set_id
        else None
    )
    allowed_sections = None
    if current_set_id:
        allowed_sections = [
            "set_overview",
            "source_memory",
            "question_index",
            "coverage_hints",
            "finding_list",
            "unfinished_work",
        ]
        if configured_set and configured_set.get("repair_targeted"):
            allowed_sections.extend(
                [
                    "source_assessments",
                    "source_findings",
                    "coverage_outcomes",
                ]
            )
    elif finalizer_mode or track_finalizer_review:
        allowed_sections = [
            "sets",
            "plan_summary",
            "deferred_sources",
            "source_map_repairs",
            "coverage_questions",
            "source_assessments",
            "source_findings",
            "coverage_hints",
        ]

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        section = str(args.get("section") or ("set_overview" if current_set_id else "sets"))
        if allowed_sections is not None and section not in allowed_sections:
            return {
                "accepted": False,
                "error": "triage_workspace_section_not_available_to_agent",
                "section": section,
                "allowed_sections": allowed_sections,
                "next_action": "Choose one allowed durable-state section for this agent role.",
            }
        cursor_text = str(args.get("cursor") or "0")
        cursor = max(0, int(cursor_text) if cursor_text.isdigit() else 0)
        limit = max(
            1,
            min(
                int(args.get("limit") or TRIAGE_WORKSPACE_DEFAULT_PAGE_LIMIT),
                TRIAGE_WORKSPACE_MAX_PAGE_LIMIT,
            ),
        )
        if section == "plan_summary":
            return _checked_page_output(_full_plan_summary_page(current, cursor=cursor))
        if section == "deferred_sources":
            return _full_deferred_source_page(current, cursor=cursor, limit=limit)
        if section == "source_map_repairs":
            return _full_source_map_repair_page(current, cursor=cursor, limit=limit)
        if section == "coverage_question_packets":
            return _coverage_question_packet_page(current, cursor=cursor, limit=limit)
        if section in {
            "coverage_questions",
            "source_read_receipts",
            "reading_notes",
            "source_assessments",
            "source_findings",
            "coverage_hints",
            "coverage_outcomes",
            "coverage_syntheses",
        }:
            payload = _full_canonical_item_page(
                current,
                section=section,
                cursor=cursor,
                limit=limit,
                current_set_id=current_set_id,
            )
            if (
                track_finalizer_review
                and section in FINALIZER_REVIEW_SECTIONS
                and payload.get("accepted") is True
            ):
                _record_finalizer_review_page(
                    current,
                    section=section,
                    page_payload=payload,
                )
                await save_source_triage_workspace(
                    db,
                    generation_run_id=run.id,
                    workspace=current,
                )
            return payload
        if section == "sets":
            payload = _workspace.workspace_summary_for_prompt(current, max_sets=limit, set_cursor=cursor)
            return fit_model_page(
                {
                    "accepted": True,
                    "section": "sets",
                    "source_sets": payload.get("source_sets") or [],
                    "page": payload.get("source_sets_page") or {},
                },
                requested_limit=limit,
                items_key="source_sets",
                error_code="triage_workspace_item_exceeds_model_read_contract",
            )

        supplied_set_id = str(args.get("set_id") or "")
        if current_set_id and supplied_set_id and supplied_set_id != current_set_id:
            return {
                "accepted": False,
                "error": "triage_source_set_scope_mismatch",
                "set_id": supplied_set_id,
                "current_set_id": current_set_id,
            }
        requested_set_id = str(
            current_set_id or supplied_set_id or ((current.get("current_set") or {}).get("set_id")) or ""
        )
        source_set = _workspace._workspace_set(current, requested_set_id) if requested_set_id else None
        if source_set is None:
            return {
                "accepted": False,
                "error": "triage_source_set_not_found",
                "set_id": requested_set_id,
                "next_action": "Page section=sets and copy an exact set_id.",
            }
        if section == "current_set_state":
            return _current_set_state_page(
                current,
                source_set,
                cursor=cursor,
                limit=limit,
            )
        if section == "set_overview":
            return set_overview_page(current, source_set, cursor=cursor, limit=limit)
        if section == "source_memory":
            source_ref = str(args.get("source_ref") or "")
            if evidence_toolset is not None:
                hydration = await hydrate_legacy_source_receipts(
                    evidence_toolset,
                    current,
                    set_id=str(source_set.get("set_id") or ""),
                    source_ref=source_ref,
                )
                if hydration.get("accepted") is not True:
                    return hydration
                if hydration.get("changed"):
                    await save_source_triage_workspace(
                        db,
                        generation_run_id=run.id,
                        workspace=current,
                    )
            return source_memory_page(
                current,
                source_set,
                source_ref=source_ref,
                cursor=cursor,
                limit=limit,
            )
        if section == "question_memory":
            return question_memory_page(
                current,
                source_set,
                question_id=str(args.get("question_id") or ""),
                cursor=cursor,
                limit=limit,
            )
        if section == "finding_catalog":
            payload = finding_catalog_page(
                current,
                source_set,
                cursor=cursor,
                limit=limit,
            )
            if current_set_id and payload.get("accepted") is True:
                payload = fit_staged_model_page(
                    payload,
                    requested_limit=limit,
                    receipt_field="finding_catalog_receipt_ref",
                    receipt_prefix="finding_catalog_receipt",
                )
                if payload.get("accepted") is not True:
                    return payload
                page = dict(payload.get("page") or {})
                receipt_ref = finding_catalog_receipt_ref(
                    set_id=str(source_set.get("set_id") or ""),
                    snapshot_id=str(payload.get("finding_catalog_snapshot_id") or ""),
                    cursor=int(str(page.get("cursor") or "0")),
                    returned_count=int(page.get("returned_count") or 0),
                )
                payload["finding_catalog_receipt_ref"] = receipt_ref
                stage_finding_catalog_page(
                    current,
                    source_set=source_set,
                    payload=payload,
                    receipt_ref=receipt_ref,
                )
                await save_source_triage_workspace(
                    db,
                    generation_run_id=run.id,
                    workspace=current,
                )
                payload["_runtime_receipt_delivery"] = {"receipt_refs": [receipt_ref]}
            return payload
        if section == "finding_list":
            return finding_catalog_page(
                current,
                source_set,
                cursor=cursor,
                limit=limit,
            )
        if section == "question_index":
            return question_index_page(
                current,
                source_set,
                cursor=cursor,
                limit=limit,
            )
        if section == "coverage_hints":
            return coverage_hints_page(
                current,
                source_set,
                source_ref=str(args.get("source_ref") or ""),
                question_id=str(args.get("question_id") or ""),
                cursor=cursor,
                limit=limit,
            )
        if section == "question_workbench":
            question_id = str(args.get("question_id") or "")
            payload = question_workbench_page(
                current,
                source_set,
                question_id=question_id,
                cursor=cursor,
                limit=limit,
            )
            if current_set_id and payload.get("accepted") is True:
                payload = fit_staged_model_page(
                    payload,
                    requested_limit=limit,
                    receipt_field="workbench_receipt_ref",
                    receipt_prefix="workbench_receipt",
                )
                if payload.get("accepted") is not True:
                    return payload
                page = dict(payload.get("page") or {})
                receipt_ref = question_workbench_receipt_ref(
                    set_id=str(source_set.get("set_id") or ""),
                    question_id=question_id,
                    snapshot_id=str(payload.get("workbench_snapshot_id") or ""),
                    cursor=int(str(page.get("cursor") or "0")),
                    returned_count=int(page.get("returned_count") or 0),
                )
                payload["workbench_receipt_ref"] = receipt_ref
                stage_question_workbench_page(
                    current,
                    source_set=source_set,
                    question_id=question_id,
                    payload=payload,
                    receipt_ref=receipt_ref,
                )
                await save_source_triage_workspace(
                    db,
                    generation_run_id=run.id,
                    workspace=current,
                )
                payload["_runtime_receipt_delivery"] = {"receipt_refs": [receipt_ref]}
            return payload
        if section == "unfinished_work":
            return unfinished_work_page(current, source_set, cursor=cursor, limit=limit)
        if section == "semantic_review":
            evidence_by_id: dict[str, dict[str, Any]] = {}
            for evidence_id in semantic_review_evidence_ids(current, source_set):
                evidence = (
                    await evidence_toolset._evidence_in_run(evidence_id)
                    if evidence_toolset is not None
                    else None
                )
                if evidence is None:
                    continue
                snippet = str(getattr(evidence, "snippet_text", "") or "")
                evidence_by_id[evidence_id] = {
                    "evidence_id": evidence_id,
                    "snippet_text": snippet,
                    "snippet_chars": len(snippet),
                    "locator": str(getattr(evidence, "locator_text", "") or ""),
                }
            payload = semantic_review_page(
                current,
                source_set,
                evidence_by_id=evidence_by_id,
                cursor=cursor,
                limit=limit,
            )
            if current_set_id and payload.get("accepted") is True:
                payload = fit_staged_model_page(
                    payload,
                    requested_limit=limit,
                    receipt_field="review_receipt_ref",
                    receipt_prefix="review_receipt",
                )
                if payload.get("accepted") is not True:
                    return payload
                page = dict(payload.get("page") or {})
                receipt_ref = semantic_review_receipt_ref(
                    set_id=str(source_set.get("set_id") or ""),
                    snapshot_id=str(payload.get("review_snapshot_id") or ""),
                    cursor=int(str(page.get("cursor") or "0")),
                    returned_count=int(page.get("returned_count") or 0),
                )
                payload["review_receipt_ref"] = receipt_ref
                stage_semantic_review_page(
                    current,
                    source_set=source_set,
                    payload=payload,
                    receipt_ref=receipt_ref,
                )
                await save_source_triage_workspace(
                    db,
                    generation_run_id=run.id,
                    workspace=current,
                )
                payload["_runtime_receipt_delivery"] = {"receipt_refs": [receipt_ref]}
            return payload
        if section == "semantic_self_check":
            return semantic_self_check_page(
                current,
                source_set,
                cursor=cursor,
                limit=limit,
            )
        if section == "current_set":
            items = [_full_source_set_item(source_set)] if cursor == 0 else []
            return _checked_page_output(
                {
                    "accepted": True,
                    "section": section,
                    "set_id": requested_set_id,
                    "items": items,
                    "page": _page_metadata(total=1, cursor=cursor, returned=len(items)),
                }
            )
        if section == "assigned_questions":
            return _full_assigned_question_page(
                current,
                source_set,
                cursor=cursor,
                limit=limit,
            )
        if section == "observations":
            observations = [item for item in source_set.get("observations") or [] if isinstance(item, dict)]
            page = observations[cursor : cursor + limit]
            items = [
                _full_triage_observation_item(
                    item,
                    observation_number=cursor + index,
                    include_evidence_ids=include_evidence_ids,
                )
                for index, item in enumerate(page, start=1)
            ]
            return fit_model_page(
                {
                    "accepted": True,
                    "section": section,
                    "set_id": requested_set_id,
                    "items": items,
                    "page": _page_metadata(total=len(observations), cursor=cursor, returned=len(items)),
                },
                requested_limit=limit,
                error_code="triage_workspace_item_exceeds_model_read_contract",
            )
        return {
            "accepted": False,
            "error": "unknown_triage_workspace_section",
            "section": section,
            "next_action": "Use one of allowed_sections and follow page.next_cursor.",
            "allowed_sections": [
                "sets",
                "plan_summary",
                "deferred_sources",
                "source_map_repairs",
                "coverage_question_packets",
                "coverage_questions",
                "source_read_receipts",
                "reading_notes",
                "source_assessments",
                "source_findings",
                "coverage_hints",
                "coverage_outcomes",
                "coverage_syntheses",
                "current_set_state",
                "set_overview",
                "source_memory",
                "finding_catalog",
                "finding_list",
                "question_memory",
                "question_index",
                "question_workbench",
                "unfinished_work",
                "semantic_review",
                "semantic_self_check",
                "current_set",
                "assigned_questions",
            ],
        }

    question_id_description = (
        "Optional for filtering coverage_hints."
        if current_set_id
        else "Required for section=question_memory/question_workbench, or optional for filtering coverage_hints."
    )

    return AgentTool(
        name="read_triage_set_state" if current_set_id else "list_triage_source_sets",
        description=(
            "Read one section of durable state for the current set. Choose the section needed and follow "
            "page.next_cursor until it is empty. Composite records may be returned as ordered, reconstructable "
            "parts keyed by owner_ref and part_kind. Semantic scalars remain whole; an individually oversized scalar "
            "returns an explicit validation error."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cursor": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": TRIAGE_WORKSPACE_MAX_PAGE_LIMIT,
                    "default": TRIAGE_WORKSPACE_DEFAULT_PAGE_LIMIT,
                    "description": (
                        "Maximum whole records or packet parts requested. The response may contain fewer when "
                        "the model-output character budget is reached; continue from page.next_cursor."
                    ),
                },
                "section": {
                    "type": "string",
                    "enum": allowed_sections or [
                        "sets",
                        "plan_summary",
                        "deferred_sources",
                        "source_map_repairs",
                        "coverage_question_packets",
                        "coverage_questions",
                        "source_read_receipts",
                        "reading_notes",
                        "source_assessments",
                        "source_findings",
                        "coverage_hints",
                        "coverage_outcomes",
                        "coverage_syntheses",
                        "current_set_state",
                        "set_overview",
                        "source_memory",
                        "finding_list",
                        "question_memory",
                        "question_index",
                        "unfinished_work",
                        "semantic_review",
                        "semantic_self_check",
                        "current_set",
                        "assigned_questions",
                    ],
                },
                "set_id": {"type": "string"},
                "source_ref": {
                    "type": "string",
                    "description": "Required only for section=source_memory or filtering coverage_hints.",
                },
                "question_id": {
                    "type": "string",
                    "description": question_id_description,
                },
            },
            "required": ["cursor", "limit"],
        },
        handler=handler,
    )


async def load_source_triage_workspace(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.load_source_triage_workspace(*args, **kwargs)


async def save_source_triage_workspace(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.save_source_triage_workspace(*args, **kwargs)


async def ensure_capsules_for_refs(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.ensure_capsules_for_refs(*args, **kwargs)


def _metadata_only_capsule(*args: Any, **kwargs: Any) -> Any:
    return _workspace._metadata_only_capsule(*args, **kwargs)


def _capsule_page_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspace._capsule_page_payload(*args, **kwargs)


def _dedupe(*args: Any, **kwargs: Any) -> Any:
    return _workspace._dedupe(*args, **kwargs)


def _stable_set_id(*args: Any, **kwargs: Any) -> Any:
    return _workspace._stable_set_id(*args, **kwargs)


def _merge_deferred(*args: Any, **kwargs: Any) -> Any:
    return _workspace._merge_deferred(*args, **kwargs)


def _workspace_set(*args: Any, **kwargs: Any) -> Any:
    return _workspace._workspace_set(*args, **kwargs)


def _replace_workspace_set(*args: Any, **kwargs: Any) -> Any:
    return _workspace._replace_workspace_set(*args, **kwargs)


def _now(*args: Any, **kwargs: Any) -> Any:
    return _workspace._now(*args, **kwargs)


def build_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    inventory: SourceMap,
    evidence_toolset: Any | None = None,
    mode: str,
    current_set_id: str | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = []
    handlers = SourceTriageWorkspaceToolHandlers(
        db=db,
        run=run,
        workspace=workspace,
        inventory=inventory,
        evidence_toolset=evidence_toolset,
        current_set_id=current_set_id,
    )

    async def planner_resume_loader() -> AgentTerminalResult | None:
        current = await load_source_triage_workspace(db, run.id) or workspace
        if current.get("status") != "planned":
            return None
        return AgentTerminalResult(
            accepted=True,
            completion=SourceTriageCompletion(
                ready=True,
                summary=str(current.get("plan_summary") or "Source triage plan already persisted."),
                notes=[],
            ),
        )

    async def set_resume_loader() -> AgentTerminalResult | None:
        current = await load_source_triage_workspace(db, run.id) or workspace
        source_set = _workspace._workspace_set(current, str(current_set_id or ""))
        if source_set is None or source_set.get("status") not in {"mapped", "deferred"}:
            return None
        deferred_reason = str(source_set.get("deferred_reason") or "").strip()
        return AgentTerminalResult(
            accepted=True,
            completion=SourceTriageCompletion(
                ready=source_set.get("status") in {"mapped", "deferred"},
                summary=str(source_set.get("findings_summary") or "Source triage set already persisted."),
                notes=[deferred_reason] if deferred_reason else [],
            ),
        )

    async def source_mapper_resume_loader() -> AgentTerminalResult | None:
        current = await load_source_triage_workspace(db, run.id) or workspace
        source_ref = str((current.get("current_source_mapping") or {}).get("source_ref") or "")
        if not source_ref:
            return None
        mapped = any(
            isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref
            for item in current.get("mapped_sources") or []
        )
        if not mapped:
            return None
        return AgentTerminalResult(
            accepted=True,
            completion=SourceTriageCompletion(
                ready=True,
                summary=f"Source {source_ref} mapping already persisted.",
                notes=[],
            ),
        )

    if mode == "planner":
        tools.append(_source_set_page_tool(db=db, run=run, workspace=workspace))
        tools.extend(
            source_triage_planner_tools(
                handlers.list_source_capsules_tool,
                handlers.route_sources_for_initial_reading_tool,
                handlers.upsert_coverage_question_tool,
                handlers.upsert_triage_source_set_tool,
                handlers.finalize_triage_workspace_plan_tool,
                terminal_resume_loader=planner_resume_loader,
            )
        )
    if mode == "set":
        active_set = _workspace._workspace_set(workspace, str(current_set_id or "")) or {}
        allowed_source_refs = [
            str(value) for value in active_set.get("source_refs") or [] if str(value)
        ]
        allowed_question_ids = [
            str(item.get("question_id") or "")
            for item in workspace.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        ]
        tools.append(
            _source_set_page_tool(
                db=db,
                run=run,
                workspace=workspace,
                current_set_id=current_set_id,
                include_evidence_ids=True,
                evidence_toolset=evidence_toolset,
            )
        )
        tools.extend(
            source_triage_set_tools(
                handlers.upsert_source_assessment_tool,
                handlers.save_coverage_hint_tool,
                handlers.finish_triage_source_set_tool,
                save_reading_note_handler=handlers.save_reading_note_tool,
                allowed_source_refs=allowed_source_refs,
                allowed_question_ids=allowed_question_ids,
                terminal_resume_loader=set_resume_loader,
            )
        )
    if mode == "source_mapper":
        active_set = _workspace._workspace_set(workspace, str(current_set_id or "")) or {}
        current_source_ref = str((workspace.get("current_source_mapping") or {}).get("source_ref") or "")
        allowed_source_refs = [current_source_ref] if current_source_ref else [
            str(value) for value in active_set.get("source_refs") or [] if str(value)
        ]
        leaf_question_ids = _leaf_question_ids_for_workspace(workspace)
        tools.extend(
            source_mapper_tools(
                handlers.upsert_source_assessment_tool,
                handlers.finish_source_mapping_tool,
                allowed_source_refs=allowed_source_refs,
                allowed_question_ids=leaf_question_ids,
                terminal_resume_loader=source_mapper_resume_loader,
            )
        )
    if mode == "finalizer":
        tools.append(
            _source_set_page_tool(
                db=db,
                run=run,
                workspace=workspace,
                finalizer_mode=True,
            )
        )
        tools.extend(
            source_triage_finalizer_tools(
                handlers.update_source_finding_links_tool,
                handlers.update_source_finding_relations_tool,
                audit_source_finding_handler=handlers.audit_source_finding_tool,
            )
        )
    return tools


def _leaf_question_ids_for_workspace(workspace: dict[str, Any]) -> list[str]:
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    parent_ids = {
        str(item.get("parent_question_id") or "")
        for item in questions
        if str(item.get("parent_question_id") or "").strip()
    }
    question_ids = [
        str(item.get("question_id") or "")
        for item in questions
        if str(item.get("question_id") or "").strip()
    ]
    leaves = [question_id for question_id in question_ids if question_id not in parent_ids]
    return leaves or question_ids
