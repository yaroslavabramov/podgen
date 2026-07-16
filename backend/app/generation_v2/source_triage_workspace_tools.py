from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as _workspace
from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.runtime import AgentTerminalResult, AgentTool
from app.generation_v2.schemas import SourceTriageCompletion
from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_lineage import coverage_synthesis_lineage
from app.generation_v2.source_triage_workspace_tool_handlers import SourceTriageWorkspaceToolHandlers
from app.generation_v2.source_triage_workspace_tool_specs import (
    source_triage_finalizer_tools,
    source_triage_planner_tools,
    source_triage_set_tools,
)
from app.models.generation_run import GenerationRun


TRIAGE_WORKSPACE_DEFAULT_PAGE_LIMIT = 20
TRIAGE_WORKSPACE_MAX_PAGE_LIMIT = 25


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
    from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash

    field_by_section = {
        "coverage_questions": "coverage_questions",
        "source_assessments": "source_assessments",
        "coverage_outcomes": "coverage_outcomes",
        "coverage_syntheses": "coverage_syntheses",
    }
    field = field_by_section[section]
    values = [item for item in workspace.get(field) or [] if isinstance(item, dict)]
    if current_set_id and section in {"source_assessments", "coverage_outcomes"}:
        values = [item for item in values if str(item.get("set_id") or "") == current_set_id]
        baseline_field = {
            "source_assessments": "repair_baseline_source_assessments",
            "coverage_outcomes": "repair_baseline_coverage_outcomes",
        }[section]
        baselines = [
            item
            for item in workspace.get(baseline_field) or []
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
        else:
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
    values.sort(key=lambda item: _canonical_item_identity(section, item))
    page = values[cursor : cursor + limit]
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
        "source_assessments": _model_safe_source_assessment,
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
    return fit_model_page(
        {
            "accepted": True,
            "section": section,
            "items": [serializers[section](item) for item in page],
            "page": _page_metadata(total=len(values), cursor=cursor, returned=len(page)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _canonical_item_identity(section: str, item: dict[str, Any]) -> tuple[str, ...]:
    """Keep offset pages stable when an upsert moves an existing record to the list tail."""

    if section == "coverage_questions":
        return (str(item.get("question_id") or ""),)
    if section == "source_assessments":
        return (
            str(item.get("source_ref") or ""),
            str(item.get("set_id") or ""),
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
    page = values[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "assigned_questions",
            "set_id": source_set.get("set_id"),
            "items": [_model_safe_coverage_question(item) for item in page],
            "page": _page_metadata(total=len(values), cursor=cursor, returned=len(page)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _model_safe_coverage_question(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": item.get("question_id"),
        "question": str(item.get("question") or ""),
        "kind": item.get("kind"),
        "importance": item.get("importance"),
        "rationale": str(item.get("rationale") or ""),
        "parent_question_id": item.get("parent_question_id"),
        "origin": item.get("origin"),
    }


def _model_safe_source_assessment(item: dict[str, Any]) -> dict[str, Any]:
    source_ref = str(item.get("source_ref") or "")
    payload = {
        "assessment_ref": f"assessment:{source_ref}" if source_ref else "",
        "set_id": item.get("set_id"),
        "source_ref": source_ref,
        "processing_status": item.get("processing_status"),
        "representation_status": item.get("representation_status"),
        "representation_reason": str(item.get("representation_reason") or ""),
        "relevance": item.get("relevance"),
        "relevance_reason": str(item.get("relevance_reason") or ""),
        "authority": item.get("authority"),
        "authority_reason": str(item.get("authority_reason") or ""),
        "uniqueness": item.get("uniqueness"),
        "uniqueness_reason": str(item.get("uniqueness_reason") or ""),
        "disposition": item.get("disposition"),
        "disposition_reason": str(item.get("disposition_reason") or ""),
        "coverage_question_ids": list(item.get("coverage_question_ids") or []),
        "editorial_functions": list(item.get("editorial_functions") or []),
        "evidence_ids": list(item.get("evidence_ids") or []),
        "limitations": list(item.get("limitations") or []),
        "confidence": item.get("confidence"),
        "updated_at": item.get("updated_at"),
    }
    if item.get("repair_baseline"):
        payload["repair_baseline"] = True
        payload["repair_baseline_version"] = int(item.get("repair_baseline_version") or 1)
        if "original_set_id" in item:
            payload["original_set_id"] = item.get("original_set_id")
        if item.get("repair_baseline_evidence_unavailable"):
            payload["repair_baseline_evidence_unavailable"] = True
            payload["repair_baseline_unavailable_evidence_count"] = int(
                item.get("repair_baseline_unavailable_evidence_count") or 0
            )
    return payload


def _model_safe_coverage_outcome(
    item: dict[str, Any],
    *,
    source_ref_by_id: dict[str, str],
    include_next_action: bool = False,
) -> dict[str, Any]:
    source_refs = [str(value) for value in item.get("source_refs") or [] if str(value).strip()]
    if not source_refs:
        source_refs = [
            source_ref_by_id[str(source_id)]
            for source_id in item.get("source_ids") or []
            if source_ref_by_id.get(str(source_id))
        ]
    source_refs = list(dict.fromkeys(source_refs))
    set_id = str(item.get("set_id") or "")
    question_id = str(item.get("question_id") or "")
    payload = {
        "outcome_ref": f"outcome:{question_id}:{set_id}" if question_id and set_id else "",
        "set_id": set_id,
        "question_id": question_id,
        "status": item.get("status"),
        "source_refs": source_refs,
        "evidence_ids": list(item.get("evidence_ids") or []),
        "orientation_summary": str(item.get("orientation_summary") or ""),
        "missing_detail": str(item.get("missing_detail") or ""),
        "conflict_summary": str(item.get("conflict_summary") or ""),
        "deferred_reason": str(item.get("deferred_reason") or ""),
        "confidence": item.get("confidence"),
        "updated_at": item.get("updated_at"),
    }
    if item.get("repair_baseline"):
        payload["repair_baseline"] = True
        payload["repair_baseline_version"] = int(item.get("repair_baseline_version") or 1)
        if item.get("repair_baseline_evidence_unavailable"):
            payload["repair_baseline_evidence_unavailable"] = True
            payload["repair_baseline_unavailable_evidence_count"] = int(
                item.get("repair_baseline_unavailable_evidence_count") or 0
            )
    if include_next_action:
        payload["next_action"] = item.get("next_action")
    return payload


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
        packet_parts.append(
            {
                "packet_ref": packet_ref,
                "part_kind": "question",
                "question": _model_safe_coverage_question(question),
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
            }
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
    page_parts = packet_parts[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "coverage_question_packets",
            "packet_contract": "ordered_parts_v1",
            "question_count": len(questions),
            "items": page_parts,
            "page": _page_metadata(total=len(packet_parts), cursor=cursor, returned=len(page_parts)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _packet_record_parts(
    *,
    packet_ref: str,
    part_kind: str,
    record: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    full_part = {
        "packet_ref": packet_ref,
        "part_kind": part_kind,
        part_kind: record,
    }
    if record is None or _packet_part_fits(full_part):
        return [full_part]

    source_refs = [str(value) for value in record.get("source_refs") or []]
    evidence_ids = [str(value) for value in record.get("evidence_ids") or []]
    semantic_record = dict(record)
    semantic_record.pop("source_refs", None)
    semantic_record.pop("evidence_ids", None)
    semantic_part = {
        "packet_ref": packet_ref,
        "part_kind": part_kind,
        part_kind: semantic_record,
        "source_ref_count": len(source_refs),
        "evidence_id_count": len(evidence_ids),
        "lineage_continues": bool(source_refs or evidence_ids),
    }
    parts = [semantic_part]
    if source_refs or evidence_ids:
        parts.extend(
            _packet_lineage_parts(
                packet_ref=packet_ref,
                owner_kind=part_kind,
                owner_ref=str(
                    record.get("outcome_ref")
                    or record.get("synthesis_ref")
                    or record.get("question_id")
                    or ""
                ),
                source_refs=source_refs,
                evidence_ids=evidence_ids,
            )
        )
    return parts


def _packet_lineage_parts(
    *,
    packet_ref: str,
    owner_kind: str,
    owner_ref: str,
    source_refs: list[str],
    evidence_ids: list[str],
) -> list[dict[str, Any]]:
    entries = [("source_ref", value) for value in source_refs]
    entries.extend(("evidence_id", value) for value in evidence_ids)
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
    cursor: int,
    total: int,
) -> dict[str, Any]:
    source_refs = [value for kind, value in entries if kind == "source_ref"]
    evidence_ids = [value for kind, value in entries if kind == "evidence_id"]
    returned = len(entries)
    return {
        "packet_ref": packet_ref,
        "part_kind": "lineage",
        "owner_kind": owner_kind,
        "owner_ref": owner_ref,
        "source_refs": source_refs,
        "evidence_ids": evidence_ids,
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
            "page": _page_metadata(total=1, cursor=0, returned=1),
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
) -> AgentTool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        section = str(args.get("section") or "sets")
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
        if section in {"coverage_questions", "source_assessments", "coverage_outcomes", "coverage_syntheses"}:
            return _full_canonical_item_page(
                current,
                section=section,
                cursor=cursor,
                limit=limit,
                current_set_id=current_set_id,
            )
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
            return {"accepted": False, "error": "triage_source_set_not_found", "set_id": requested_set_id}
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
        return {"accepted": False, "error": "unknown_triage_workspace_section", "section": section}

    return AgentTool(
        name="list_triage_source_sets",
        description=(
            "Page compact source-set listings or full durable records after compact context/checkpoint "
            "truncation. Finalizers should use coverage_question_packets and follow next_cursor through its "
            "ordered question, existing-synthesis, per-set-outcome, and optional lineage parts. Semantic "
            "records are always whole; large packets continue in this same section."
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
                    "enum": [
                        "sets",
                        "plan_summary",
                        "deferred_sources",
                        "source_map_repairs",
                        "coverage_question_packets",
                        "coverage_questions",
                        "source_assessments",
                        "coverage_outcomes",
                        "coverage_syntheses",
                        "current_set",
                        "assigned_questions",
                        "observations",
                    ],
                },
                "set_id": {"type": "string"},
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
        if source_set is None or source_set.get("status") not in {"mapped", "deferred", "needs_repair"}:
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
        tools.append(
            _source_set_page_tool(
                db=db,
                run=run,
                workspace=workspace,
                current_set_id=current_set_id,
                include_evidence_ids=True,
            )
        )
        tools.extend(
            source_triage_set_tools(
                handlers.record_triage_observation_tool,
                handlers.upsert_source_assessment_tool,
                handlers.upsert_coverage_outcome_tool,
                handlers.finish_triage_source_set_tool,
                terminal_resume_loader=set_resume_loader,
            )
        )
    if mode == "finalizer":
        tools.append(_source_set_page_tool(db=db, run=run, workspace=workspace))
        tools.extend(
            source_triage_finalizer_tools(
                handlers.finish_triage_source_set_tool,
                handlers.upsert_source_assessment_tool,
                handlers.upsert_coverage_outcome_tool,
                handlers.upsert_coverage_synthesis_tool,
            )
        )
    return tools
