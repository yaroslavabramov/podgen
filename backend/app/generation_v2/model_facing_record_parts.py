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
    if record_kind == "source_finding":
        return _source_finding_parts(value, owner)
    if record_kind in {"coverage_outcome", "coverage_synthesis"}:
        return _coverage_outcome_parts(value, owner, record_kind=record_kind)
    if record_kind == "source_assessment":
        return _source_assessment_parts(value, owner)
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
    core = _core_part(
        owner,
        "source_assessment",
        record,
        nested_counts={field: len(values) for field, values in lists.items()},
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
    return parts


def _source_finding_parts(record: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    evidence_ids = list(record.pop("evidence_ids", []) or [])
    limitations = list(record.pop("limitations", []) or [])
    question_links = [item for item in record.pop("question_links", []) if isinstance(item, dict)]
    source_relations = [item for item in record.pop("source_relations", []) if isinstance(item, dict)]
    scope = dict(record.get("scope") or {})
    qualifiers = list(scope.pop("qualifiers", []) or [])
    record["scope"] = scope
    core = _core_part(
        owner,
        "source_finding",
        record,
        nested_counts={
            "evidence_ids": len(evidence_ids),
            "scope.qualifiers": len(qualifiers),
            "limitations": len(limitations),
            "question_links": len(question_links),
            "source_relations": len(source_relations),
        },
    )
    parts = [core]
    for part_kind, values in (
        ("finding_evidence_ids", evidence_ids),
        ("finding_scope_qualifiers", qualifiers),
        ("finding_limitations", limitations),
    ):
        parts.extend(
            _list_parts(
                owner=owner,
                record_kind="source_finding",
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
                record_kind="source_finding",
                part_kind="finding_question_link_requirement_ids",
                values=requirement_ids,
                part_ref=link_ref,
                owner_fields={"question_id": question_id},
            )
        )
    parts.extend(
        _list_parts(
            owner=owner,
            record_kind="source_finding",
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
                record_kind="source_finding",
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
            record_kind="source_finding",
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
        },
    )
    parts = [core]
    for field, values in lists.items():
        parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind=f"outcome_{field}",
                values=values,
            )
        )

    result_cores: list[dict[str, Any]] = []
    result_finding_parts: list[dict[str, Any]] = []
    for index, raw_result in enumerate(requirement_results):
        result = dict(raw_result)
        finding_ids = list(result.pop("finding_ids", []) or [])
        requirement_id = str(result.get("requirement_id") or index)
        result_ref = f"{owner}:requirement_result:{requirement_id}"
        result["result_ref"] = result_ref
        result["finding_id_count"] = len(finding_ids)
        result_cores.append(result)
        result_finding_parts.extend(
            _list_parts(
                owner=owner,
                record_kind=record_kind,
                part_kind="outcome_requirement_result_finding_ids",
                values=finding_ids,
                part_ref=result_ref,
                owner_fields={"requirement_id": requirement_id},
            )
        )
    parts.extend(
        _list_parts(
            owner=owner,
            record_kind=record_kind,
            part_kind="outcome_requirement_results",
            values=result_cores,
        )
    )
    parts.extend(result_finding_parts)
    return parts


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
