from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS


# Leave room for the surrounding tool/page envelope and packet ownership fields.
_ITEM_BUDGET_CHARS = MODEL_TOOL_OUTPUT_MAX_CHARS - 1_200


def composite_record_parts(
    record: dict[str, Any],
    *,
    record_kind: str,
    owner_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Expose a validated composite record as whole, reconstructable model-facing parts."""

    value = deepcopy(record)
    owner = owner_ref or _default_owner_ref(record_kind, value)
    full = {
        "owner_ref": owner,
        "record_kind": record_kind,
        "part_kind": "full_record",
        **value,
    }
    if _item_fits(full):
        return [full]
    if record_kind == "coverage_question":
        return _coverage_question_parts(value, owner)
    if record_kind in {"source_finding", "candidate_source_finding"}:
        return _source_finding_parts(value, owner, record_kind=record_kind)
    if record_kind in {"coverage_outcome", "coverage_synthesis"}:
        return _coverage_outcome_parts(value, owner, record_kind=record_kind)
    if record_kind == "source_assessment":
        return _source_assessment_parts(value, owner)
    if record_kind == "source_structure_page":
        return _source_structure_page_parts(value, owner)
    if record_kind == "source_read_receipt":
        return _source_read_receipt_parts(value, owner)
    return [full]


def _coverage_question_parts(record: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    requirements = [item for item in record.pop("answer_requirements", []) if isinstance(item, dict)]
    core = _core_part(
        owner,
        "coverage_question",
        record,
        nested_counts={"answer_requirements": len(requirements)},
    )
    return [
        core,
        *_list_parts(
            owner=owner,
            record_kind="coverage_question",
            part_kind="answer_requirements",
            values=requirements,
        ),
    ]


def _source_assessment_parts(record: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    list_fields = (
        "coverage_question_ids",
        "editorial_functions",
        "evidence_ids",
        "finding_ids",
        "limitations",
    )
    lists = {field: list(record.pop(field, []) or []) for field in list_fields}
    reading_basis = dict(record.pop("reading_basis", {}) or {})
    core = _core_part(
        owner,
        "source_assessment",
        record,
        nested_counts={
            **{field: len(values) for field, values in lists.items()},
            "reading_basis": int(bool(reading_basis)),
        },
    )
    parts = [core]
    for field, values in lists.items():
        parts.extend(
            _list_parts(
                owner=owner,
                record_kind="source_assessment",
                part_kind=f"assessment_{field}",
                values=values,
            )
        )
    parts.extend(_assessment_reading_basis_parts(reading_basis, owner=owner))
    return parts


def _source_structure_page_parts(record: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    items = [item for item in record.pop("items", []) if isinstance(item, dict)]
    return [
        _core_part(
            owner,
            "source_structure_page",
            record,
            nested_counts={"items": len(items)},
        ),
        *_list_parts(
            owner=owner,
            record_kind="source_structure_page",
            part_kind="source_structure_items",
            values=items,
        ),
    ]


def _source_read_receipt_parts(record: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    passage_refs = list(record.pop("passage_refs", []) or [])
    replay_passages = [item for item in record.pop("replay_passages", []) if isinstance(item, dict)]
    parts = [
        _core_part(
            owner,
            "source_read_receipt",
            record,
            nested_counts={
                "passage_refs": len(passage_refs),
                "replay_passages": len(replay_passages),
            },
        )
    ]
    parts.extend(
        _list_parts(
            owner=owner,
            record_kind="source_read_receipt",
            part_kind="receipt_passage_refs",
            values=passage_refs,
        )
    )
    parts.extend(
        _list_parts(
            owner=owner,
            record_kind="source_read_receipt",
            part_kind="receipt_replay_passages",
            values=replay_passages,
        )
    )
    return parts


def _assessment_reading_basis_parts(
    reading_basis: dict[str, Any],
    *,
    owner: str,
) -> list[dict[str, Any]]:
    if not reading_basis:
        return []
    basis_ref = f"{owner}:reading_basis"
    ledger = dict(reading_basis.pop("coverage_ledger", {}) or {})
    semantic_fields = {
        field: reading_basis.pop(field)
        for field in (
            "coverage_summary",
            "unread_regions",
            "unread_risk_assessment",
            "representation_limits",
            "stopping_reason",
        )
        if field in reading_basis
    }
    parts = [
        {
            "owner_ref": owner,
            "record_kind": "source_assessment",
            "part_kind": "assessment_reading_basis_core",
            "part_ref": basis_ref,
            **reading_basis,
            "nested_counts": {
                "semantic_fields": len(semantic_fields),
                "coverage_ledger": int(bool(ledger)),
            },
        }
    ]
    parts.extend(
        {
            "owner_ref": owner,
            "record_kind": "source_assessment",
            "part_kind": "assessment_reading_basis_field",
            "part_ref": basis_ref,
            "field": field,
            "value": value,
        }
        for field, value in semantic_fields.items()
    )
    parts.extend(_assessment_coverage_ledger_parts(ledger, owner=owner))
    return parts


def _assessment_coverage_ledger_parts(
    ledger: dict[str, Any],
    *,
    owner: str,
) -> list[dict[str, Any]]:
    if not ledger:
        return []
    ledger_ref = f"{owner}:reading_basis:coverage_ledger"
    structure = dict(ledger.pop("structure", {}) or {})
    list_fields = ("search_ranges", "search_queries", "sequential_ranges")
    lists = {field: list(ledger.pop(field, []) or []) for field in list_fields}
    parts = [
        {
            "owner_ref": owner,
            "record_kind": "source_assessment",
            "part_kind": "assessment_coverage_ledger_core",
            "part_ref": ledger_ref,
            **ledger,
            "nested_counts": {
                **{field: len(values) for field, values in lists.items()},
                "structure": int(bool(structure)),
            },
        }
    ]
    for field, values in lists.items():
        parts.extend(
            _list_parts(
                owner=owner,
                record_kind="source_assessment",
                part_kind=f"assessment_coverage_ledger_{field}",
                values=values,
                part_ref=ledger_ref,
            )
        )
    parts.extend(_assessment_structure_parts(structure, owner=owner, ledger_ref=ledger_ref))
    return parts


def _assessment_structure_parts(
    structure: dict[str, Any],
    *,
    owner: str,
    ledger_ref: str,
) -> list[dict[str, Any]]:
    if not structure:
        return []
    structure_ref = f"{ledger_ref}:structure"
    items = list(structure.pop("items", []) or [])
    return [
        {
            "owner_ref": owner,
            "record_kind": "source_assessment",
            "part_kind": "assessment_coverage_structure_core",
            "part_ref": structure_ref,
            **structure,
            "nested_counts": {"items": len(items)},
        },
        *_list_parts(
            owner=owner,
            record_kind="source_assessment",
            part_kind="assessment_coverage_structure_items",
            values=items,
            part_ref=structure_ref,
        ),
    ]


def _source_finding_parts(
    record: dict[str, Any],
    owner: str,
    *,
    record_kind: str = "source_finding",
) -> list[dict[str, Any]]:
    evidence_ids = list(record.pop("evidence_ids", []) or [])
    passage_refs = list(record.pop("passage_refs", []) or [])
    possible_question_ids = list(record.pop("possible_question_ids", []) or [])
    evidence_previews = [
        item for item in record.pop("evidence_previews", []) if isinstance(item, dict)
    ]
    limitations = list(record.pop("limitations", []) or [])
    question_links = [item for item in record.pop("question_links", []) if isinstance(item, dict)]
    source_relations = [item for item in record.pop("source_relations", []) if isinstance(item, dict)]
    scope = dict(record.get("scope") or {})
    qualifiers = list(scope.pop("qualifiers", []) or [])
    record["scope"] = scope
    core = _core_part(
        owner,
        record_kind,
        record,
        nested_counts={
            "evidence_ids": len(evidence_ids),
            "passage_refs": len(passage_refs),
            "possible_question_ids": len(possible_question_ids),
            "evidence_previews": len(evidence_previews),
            "scope.qualifiers": len(qualifiers),
            "limitations": len(limitations),
            "question_links": len(question_links),
            "source_relations": len(source_relations),
        },
    )
    parts = [core]
    for part_kind, values in (
        ("finding_evidence_ids", evidence_ids),
        ("finding_passage_refs", passage_refs),
        ("finding_possible_question_ids", possible_question_ids),
        ("finding_evidence_previews", evidence_previews),
        ("finding_scope_qualifiers", qualifiers),
        ("finding_limitations", limitations),
    ):
        parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind=part_kind,
                values=values,
            )
        )

    link_cores: list[dict[str, Any]] = []
    link_requirement_parts: list[dict[str, Any]] = []
    for index, raw_link in enumerate(question_links):
        link = dict(raw_link)
        requirement_ids = list(link.pop("requirement_ids", []) or [])
        question_id = str(link.get("question_id") or index)
        link_ref = f"{owner}:question_link:{question_id}"
        link["link_ref"] = link_ref
        link["requirement_id_count"] = len(requirement_ids)
        link_cores.append(link)
        link_requirement_parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind="finding_question_link_requirement_ids",
                values=requirement_ids,
                part_ref=link_ref,
                owner_fields={"question_id": question_id},
            )
        )
    parts.extend(
        _list_parts(
            owner=owner,
            record_kind=record_kind,
            part_kind="finding_question_links",
            values=link_cores,
        )
    )
    parts.extend(link_requirement_parts)

    relation_cores: list[dict[str, Any]] = []
    relation_evidence_parts: list[dict[str, Any]] = []
    for index, raw_relation in enumerate(source_relations):
        relation = dict(raw_relation)
        relation_evidence_ids = list(relation.pop("evidence_ids", []) or [])
        related = str(
            relation.get("related_source_ref") or relation.get("related_source_id") or index
        )
        relationship = str(relation.get("relationship") or "relation")
        relation_ref = f"{owner}:source_relation:{related}:{relationship}:{index}"
        relation["relation_ref"] = relation_ref
        relation["evidence_id_count"] = len(relation_evidence_ids)
        relation_cores.append(relation)
        relation_evidence_parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind="finding_source_relation_evidence_ids",
                values=relation_evidence_ids,
                part_ref=relation_ref,
                owner_fields={
                    "related_source_ref": related,
                    "relationship": relationship,
                },
            )
        )
    parts.extend(
        _list_parts(
            owner=owner,
            record_kind=record_kind,
            part_kind="finding_source_relations",
            values=relation_cores,
        )
    )
    parts.extend(relation_evidence_parts)
    return parts


def _coverage_outcome_parts(
    record: dict[str, Any],
    owner: str,
    *,
    record_kind: str,
) -> list[dict[str, Any]]:
    list_fields = ("source_refs", "evidence_ids", "finding_ids")
    lists = {field: list(record.pop(field, []) or []) for field in list_fields}
    semantic_field_names = (
        "orientation_summary",
        "missing_detail",
        "conflict_summary",
        "deferred_reason",
    )
    semantic_fields = {
        field: str(record.pop(field) or "")
        for field in semantic_field_names
        if field in record
    }
    requirement_results = [
        item for item in record.pop("requirement_results", []) if isinstance(item, dict)
    ]
    core = _core_part(
        owner,
        record_kind,
        record,
        nested_counts={
            **{field: len(values) for field, values in lists.items()},
            "requirement_results": len(requirement_results),
            "semantic_fields": len(semantic_fields),
        },
    )
    parts = [core]
    for field, value in semantic_fields.items():
        parts.extend(
            _text_field_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind="outcome_semantic_field",
                field=field,
                value=value,
            )
        )
    for field, values in lists.items():
        parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind=f"outcome_{field}",
                values=values,
            )
        )

    result_core_parts: list[dict[str, Any]] = []
    result_detail_parts: list[dict[str, Any]] = []
    for index, raw_result in enumerate(requirement_results):
        result = dict(raw_result)
        finding_ids = list(result.pop("finding_ids", []) or [])
        answer_points = [
            item for item in (result.pop("answer_points", []) or []) if isinstance(item, dict)
        ]
        result_semantic_fields = {
            field: str(result.pop(field) or "")
            for field in (
                "summary",
                "closure_basis",
                "remaining_uncertainty",
                "missing_detail",
                "conflict_summary",
                "deferred_reason",
            )
            if field in result
        }
        requirement_id = str(result.get("requirement_id") or index)
        result_ref = f"{owner}:requirement_result:{requirement_id}"
        result["result_ref"] = result_ref
        result["nested_counts"] = {
            "finding_ids": len(finding_ids),
            "answer_points": len(answer_points),
            "semantic_fields": len(result_semantic_fields),
        }
        result_core_parts.append(
            {
                "owner_ref": owner,
                "record_kind": record_kind,
                "part_kind": "outcome_requirement_result_core",
                **result,
            }
        )
        result_detail_parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind="outcome_requirement_result_finding_ids",
                values=finding_ids,
                part_ref=result_ref,
                owner_fields={"requirement_id": requirement_id},
            )
        )
        for field, value in result_semantic_fields.items():
            result_detail_parts.extend(
                _text_field_parts(
                    owner=owner,
                    record_kind=record_kind,
                    part_kind="outcome_requirement_result_semantic_field",
                    field=field,
                    value=value,
                    part_ref=f"{result_ref}:{field}",
                    owner_fields={
                        "requirement_id": requirement_id,
                        "result_ref": result_ref,
                    },
                )
            )
        result_detail_parts.extend(
            _answer_point_parts(
                answer_points,
                owner=owner,
                record_kind=record_kind,
                result_ref=result_ref,
                requirement_id=requirement_id,
            )
        )
    parts.extend(result_core_parts)
    parts.extend(result_detail_parts)
    return parts


def _answer_point_parts(
    answer_points: list[dict[str, Any]],
    *,
    owner: str,
    record_kind: str,
    result_ref: str,
    requirement_id: str,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for index, raw_point in enumerate(answer_points):
        point = dict(raw_point)
        text = str(point.pop("point", "") or "")
        finding_ids = list(point.pop("finding_ids", []) or [])
        point_ref = f"{result_ref}:answer_point:{index}"
        parts.append(
            {
                "owner_ref": owner,
                "record_kind": record_kind,
                "part_kind": "outcome_requirement_answer_point_core",
                "part_ref": point_ref,
                "requirement_id": requirement_id,
                "answer_point_index": index,
                **point,
                "nested_counts": {
                    "point": int(bool(text)),
                    "finding_ids": len(finding_ids),
                },
            }
        )
        parts.extend(
            _text_field_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind="outcome_requirement_answer_point_text",
                field="point",
                value=text,
                part_ref=f"{point_ref}:point",
                owner_fields={
                    "requirement_id": requirement_id,
                    "answer_point_index": index,
                    "point_ref": point_ref,
                },
            )
        )
        parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind="outcome_requirement_answer_point_finding_ids",
                values=finding_ids,
                part_ref=f"{point_ref}:finding_ids",
                owner_fields={
                    "requirement_id": requirement_id,
                    "answer_point_index": index,
                    "point_ref": point_ref,
                },
            )
        )
    return parts


def _text_field_parts(
    *,
    owner: str,
    record_kind: str,
    part_kind: str,
    field: str,
    value: str,
    part_ref: str | None = None,
    owner_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Page one semantic string without dropping or shortening any character."""

    total = len(value)
    if total == 0:
        return [
            _text_field_part(
                owner=owner,
                record_kind=record_kind,
                part_kind=part_kind,
                field=field,
                value="",
                cursor=0,
                total=0,
                part_ref=part_ref,
                owner_fields=owner_fields,
            )
        ]

    cursor = 0
    parts: list[dict[str, Any]] = []
    while cursor < total:
        low = cursor + 1
        high = total
        best_end = low
        while low <= high:
            end = (low + high) // 2
            candidate = _text_field_part(
                owner=owner,
                record_kind=record_kind,
                part_kind=part_kind,
                field=field,
                value=value[cursor:end],
                cursor=cursor,
                total=total,
                part_ref=part_ref,
                owner_fields=owner_fields,
            )
            if _item_fits(candidate):
                best_end = end
                low = end + 1
            else:
                high = end - 1
        parts.append(
            _text_field_part(
                owner=owner,
                record_kind=record_kind,
                part_kind=part_kind,
                field=field,
                value=value[cursor:best_end],
                cursor=cursor,
                total=total,
                part_ref=part_ref,
                owner_fields=owner_fields,
            )
        )
        cursor = best_end
    return parts


def _text_field_part(
    *,
    owner: str,
    record_kind: str,
    part_kind: str,
    field: str,
    value: str,
    cursor: int,
    total: int,
    part_ref: str | None = None,
    owner_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    end = cursor + len(value)
    return {
        "owner_ref": owner,
        "record_kind": record_kind,
        "part_kind": part_kind,
        "part_ref": part_ref or f"{owner}:{part_kind}:{field}",
        **dict(owner_fields or {}),
        "field": field,
        "value": value,
        "part_page": {
            "unit": "characters",
            "total_count": total,
            "returned_count": len(value),
            "omitted_count": max(0, total - end),
            "cursor": str(cursor),
            "next_cursor": str(end) if end < total else "",
        },
    }


def _core_part(
    owner: str,
    record_kind: str,
    record: dict[str, Any],
    *,
    nested_counts: dict[str, int],
) -> dict[str, Any]:
    return {
        "owner_ref": owner,
        "record_kind": record_kind,
        "part_kind": "core",
        **record,
        "nested_counts": nested_counts,
    }


def _list_parts(
    *,
    owner: str,
    record_kind: str,
    part_kind: str,
    values: list[Any],
    part_ref: str | None = None,
    owner_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not values:
        return []
    stable_part_ref = part_ref or f"{owner}:{part_kind}"
    owner_fields = dict(owner_fields or {})
    total = len(values)
    cursor = 0
    parts: list[dict[str, Any]] = []
    while cursor < total:
        returned = 0
        best: dict[str, Any] | None = None
        while cursor + returned < total:
            count = returned + 1
            candidate = _list_part(
                owner=owner,
                record_kind=record_kind,
                part_kind=part_kind,
                part_ref=stable_part_ref,
                owner_fields=owner_fields,
                values=values[cursor : cursor + count],
                cursor=cursor,
                total=total,
            )
            if not _item_fits(candidate):
                break
            best = candidate
            returned = count
        if best is None:
            # A single scalar/nested item violates the read contract. Keep it
            # whole so the outer pager returns an explicit validation error.
            best = _list_part(
                owner=owner,
                record_kind=record_kind,
                part_kind=part_kind,
                part_ref=stable_part_ref,
                owner_fields=owner_fields,
                values=values[cursor : cursor + 1],
                cursor=cursor,
                total=total,
            )
            returned = 1
        parts.append(best)
        cursor += returned
    return parts


def _list_part(
    *,
    owner: str,
    record_kind: str,
    part_kind: str,
    part_ref: str,
    owner_fields: dict[str, Any],
    values: list[Any],
    cursor: int,
    total: int,
) -> dict[str, Any]:
    returned = len(values)
    return {
        "owner_ref": owner,
        "record_kind": record_kind,
        "part_kind": part_kind,
        "part_ref": part_ref,
        **owner_fields,
        "items": values,
        "part_page": {
            "total_count": total,
            "returned_count": returned,
            "omitted_count": max(0, total - cursor - returned),
            "cursor": str(cursor),
            "next_cursor": str(cursor + returned) if cursor + returned < total else "",
        },
    }


def _default_owner_ref(record_kind: str, record: dict[str, Any]) -> str:
    identity_fields: dict[str, tuple[str, ...]] = {
        "coverage_question": ("question_id",),
        "source_assessment": ("assessment_ref", "assessment_id", "source_ref"),
        "source_finding": ("finding_id",),
        "coverage_outcome": ("outcome_ref", "outcome_id", "question_id"),
        "coverage_synthesis": ("synthesis_ref", "outcome_id", "question_id"),
    }
    value = next(
        (
            str(record.get(field) or "")
            for field in identity_fields.get(record_kind, ())
            if str(record.get(field) or "")
        ),
        "unknown",
    )
    public_prefixes = {
        "coverage_question": "coverage_question:",
        "source_assessment": "assessment:",
        "source_finding": "finding:",
        "coverage_outcome": "outcome:",
        "coverage_synthesis": "synthesis:",
    }
    public_prefix = public_prefixes.get(record_kind, "")
    if public_prefix and value.startswith(public_prefix):
        return value
    return f"{record_kind}:{value}"


def _item_fits(value: dict[str, Any]) -> bool:
    return len(json.dumps(value, ensure_ascii=False, default=str)) <= _ITEM_BUDGET_CHARS


__all__ = ["composite_record_parts"]
