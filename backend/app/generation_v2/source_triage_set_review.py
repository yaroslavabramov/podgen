from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.source_triage_set_review_content import (
    semantic_review_evidence_ids,
    semantic_review_page,
)
from app.generation_v2.source_triage_set_state_views import _page_metadata, _set_records


SET_SEMANTIC_REVIEWS_FIELD = "source_triage_set_semantic_reviews"
SET_SEMANTIC_SELF_CHECKS_FIELD = "source_triage_set_semantic_self_checks"


def record_semantic_review_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    page: dict[str, Any],
) -> None:
    if page.get("accepted") is not True:
        return
    signature = str(page.get("review_snapshot_id") or "")
    current_signature = set_semantic_review_signature(workspace, source_set)
    if not signature or signature != current_signature:
        return
    page_meta = dict(page.get("page") or {})
    cursor = _non_negative_int(page_meta.get("cursor"))
    returned = _non_negative_int(page_meta.get("returned_count"))
    total = _non_negative_int(page_meta.get("total_count"))
    reviews = dict(workspace.get(SET_SEMANTIC_REVIEWS_FIELD) or {})
    existing = reviews.get(str(source_set.get("set_id") or ""))
    read_indexes = (
        {int(value) for value in existing.get("read_part_indexes") or []}
        if isinstance(existing, dict) and str(existing.get("signature") or "") == signature
        else set()
    )
    read_indexes.update(range(cursor, min(total, cursor + returned)))
    reviews[str(source_set.get("set_id") or "")] = {
        "signature": signature,
        "total_part_count": total,
        "read_part_indexes": sorted(read_indexes),
        "complete": total == 0 or len(read_indexes) == total,
    }
    workspace[SET_SEMANTIC_REVIEWS_FIELD] = reviews


def set_semantic_review_signature(workspace: dict[str, Any], source_set: dict[str, Any]) -> str:
    set_id = str(source_set.get("set_id") or "")
    question_ids = semantic_review_question_ids(workspace, source_set)
    semantic = {
        "set_id": set_id,
        "output_language": str((workspace.get("intent") or {}).get("language") or "").strip(),
        "questions": [
            _without_operational_fields(item)
            for item in workspace.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "") in question_ids
        ],
        "assessments": [
            _without_operational_fields(item)
            for item in _set_records(workspace, "source_assessments", set_id)
        ],
        "findings": [
            _without_operational_fields(item)
            for item in _set_records(workspace, "source_findings", set_id)
        ],
        "outcomes": [
            _without_operational_fields(item)
            for item in _set_records(workspace, "coverage_outcomes", set_id)
        ],
    }
    return sha256(
        json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def semantic_review_question_ids(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> set[str]:
    """Return local questions, their ancestors, and descendants of local roots.

    Adding an ancestor must not pull in sibling branches owned by another set.
    A locally owned broad question still includes its complete descendant tree.
    """

    set_id = str(source_set.get("set_id") or "")
    question_ids = {
        *[str(value) for value in source_set.get("question_ids") or [] if str(value)],
        *[
            str(link.get("question_id") or "")
            for finding in _set_records(workspace, "source_findings", set_id)
            for link in finding.get("question_links") or []
            if isinstance(link, dict) and str(link.get("question_id") or "")
        ],
        *[
            str(item.get("question_id") or "")
            for item in _set_records(workspace, "coverage_outcomes", set_id)
            if str(item.get("question_id") or "")
        ],
    }
    seed_ids = set(question_ids)
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    parent_by_child = {
        str(item.get("question_id") or ""): str(item.get("parent_question_id") or "")
        for item in questions
        if str(item.get("question_id") or "")
    }
    children_by_parent: dict[str, set[str]] = {}
    for child_id, parent_id in parent_by_child.items():
        if parent_id:
            children_by_parent.setdefault(parent_id, set()).add(child_id)

    ancestor_frontier = list(seed_ids)
    while ancestor_frontier:
        parent_id = parent_by_child.get(ancestor_frontier.pop()) or ""
        if parent_id and parent_id not in question_ids:
            question_ids.add(parent_id)
            ancestor_frontier.append(parent_id)

    descendant_frontier = list(seed_ids)
    while descendant_frontier:
        for child_id in children_by_parent.get(descendant_frontier.pop(), set()):
            if child_id not in question_ids:
                question_ids.add(child_id)
                descendant_frontier.append(child_id)
    return question_ids


def semantic_review_question_relations(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> set[tuple[str, str]]:
    question_ids = semantic_review_question_ids(workspace, source_set)
    return {
        (str(item.get("parent_question_id") or ""), str(item.get("question_id") or ""))
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict)
        and str(item.get("parent_question_id") or "") in question_ids
        and str(item.get("question_id") or "") in question_ids
    }


def set_semantic_review_is_current(workspace: dict[str, Any], source_set: dict[str, Any]) -> bool:
    review = (workspace.get(SET_SEMANTIC_REVIEWS_FIELD) or {}).get(
        str(source_set.get("set_id") or "")
    )
    return bool(
        isinstance(review, dict)
        and review.get("complete") is True
        and str(review.get("signature") or "") == set_semantic_review_signature(workspace, source_set)
    )


def set_semantic_self_check_is_current(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> bool:
    self_check = (workspace.get(SET_SEMANTIC_SELF_CHECKS_FIELD) or {}).get(
        str(source_set.get("set_id") or "")
    )
    return bool(
        isinstance(self_check, dict)
        and str(self_check.get("contract_version") or "") == "v2.semantic_review_ack.1"
        and str(self_check.get("review_snapshot_id") or "")
        == set_semantic_review_signature(workspace, source_set)
    )


def semantic_self_check_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    self_check = (workspace.get(SET_SEMANTIC_SELF_CHECKS_FIELD) or {}).get(set_id)
    if not isinstance(self_check, dict):
        return {
            "accepted": False,
            "error": "semantic_self_check_not_saved",
            "set_id": set_id,
            "next_action": (
                "Read the current semantic_review completely, then call acknowledge_semantic_review."
            ),
        }
    items: list[dict[str, Any]] = [
        {
            "part_kind": "self_check_core",
            "set_id": set_id,
            "review_snapshot_id": self_check.get("review_snapshot_id"),
            "contract_version": self_check.get("contract_version"),
            "output_language": self_check.get("output_language"),
            "readiness_explanation": self_check.get("readiness_explanation"),
            "created_at": self_check.get("created_at"),
            "is_current": set_semantic_self_check_is_current(workspace, source_set),
            "legacy_record": str(self_check.get("contract_version") or "").startswith(
                "v2.semantic_self_check."
            ),
        },
        *[
            {"part_kind": "finding_check", **item}
            for item in self_check.get("finding_checks") or []
            if isinstance(item, dict)
        ],
        *[
            {"part_kind": "finding_link_check", **item}
            for item in self_check.get("finding_link_checks") or []
            if isinstance(item, dict)
        ],
        *[
            {"part_kind": "requirement_check", **item}
            for item in self_check.get("requirement_checks") or []
            if isinstance(item, dict)
        ],
        *[
            {"part_kind": "source_reading_check", **item}
            for item in self_check.get("source_reading_checks") or []
            if isinstance(item, dict)
        ],
        *[
            {"part_kind": "outcome_check", **item}
            for item in self_check.get("outcome_checks") or []
            if isinstance(item, dict)
        ],
        *[
            {"part_kind": "question_relation_check", **item}
            for item in self_check.get("question_relation_checks") or []
            if isinstance(item, dict)
        ],
    ]
    page_items = items[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "semantic_self_check",
            "set_id": set_id,
            "items": page_items,
            "page": {
                **_page_metadata(total=len(items), cursor=cursor, returned=len(page_items)),
                "unit": "self_check_part",
            },
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def semantic_review_progress(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> dict[str, Any]:
    """Expose exact unread review ranges without making a semantic judgment."""

    set_id = str(source_set.get("set_id") or "")
    signature = set_semantic_review_signature(workspace, source_set)
    review = (workspace.get(SET_SEMANTIC_REVIEWS_FIELD) or {}).get(set_id)
    if not isinstance(review, dict) or str(review.get("signature") or "") != signature:
        return {
            "review_snapshot_id": signature,
            "snapshot_started": False,
            "complete": False,
            "read_part_count": 0,
            "total_part_count": 0,
            "remaining_part_count": None,
            "next_unread_cursor": "0",
            "unread_ranges": [],
            "semantic_self_check_current": False,
        }
    total = _non_negative_int(review.get("total_part_count"))
    read_indexes = {
        int(value)
        for value in review.get("read_part_indexes") or []
        if str(value).isdigit() and 0 <= int(value) < total
    }
    unread_indexes = [index for index in range(total) if index not in read_indexes]
    unread_ranges = _contiguous_ranges(unread_indexes)
    return {
        "review_snapshot_id": signature,
        "snapshot_started": True,
        "complete": not unread_indexes,
        "read_part_count": len(read_indexes),
        "total_part_count": total,
        "remaining_part_count": len(unread_indexes),
        "next_unread_cursor": str(unread_indexes[0]) if unread_indexes else "",
        "unread_ranges": [
            {
                "start_cursor": str(start),
                "end_cursor_exclusive": str(end),
                "part_count": end - start,
            }
            for start, end in unread_ranges[:12]
        ],
        "omitted_unread_range_count": max(0, len(unread_ranges) - 12),
        "semantic_self_check_current": set_semantic_self_check_is_current(workspace, source_set),
    }


def _contiguous_ranges(indexes: list[int]) -> list[tuple[int, int]]:
    if not indexes:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = indexes[0]
    for index in indexes[1:]:
        if index == previous + 1:
            previous = index
            continue
        ranges.append((start, previous + 1))
        start = previous = index
    ranges.append((start, previous + 1))
    return ranges


def _without_operational_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_operational_fields(item)
            for key, item in value.items()
            if key not in {"created_at", "updated_at", "semantic_revision"}
        }
    if isinstance(value, list):
        return [_without_operational_fields(item) for item in value]
    return value


def _non_negative_int(value: Any) -> int:
    text = str(value or "0")
    return max(0, int(text) if text.isdigit() else 0)


__all__ = [
    "record_semantic_review_page",
    "semantic_review_progress",
    "semantic_review_evidence_ids",
    "semantic_review_page",
    "semantic_review_question_ids",
    "semantic_review_question_relations",
    "set_semantic_review_is_current",
    "set_semantic_review_signature",
]
