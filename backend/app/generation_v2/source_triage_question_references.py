from __future__ import annotations

from typing import Any, Iterable


def known_question_id_error(
    workspace: dict[str, Any],
    supplied_id: str,
    *,
    error: str,
) -> dict[str, Any]:
    questions = [
        item
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict) and str(item.get("question_id") or "")
    ]
    question_ids = sorted(str(item.get("question_id") or "") for item in questions)
    owning_question_ids = sorted(
        {
            str(question.get("question_id") or "")
            for question in questions
            for requirement in question.get("answer_requirements") or []
            if isinstance(requirement, dict)
            and str(requirement.get("requirement_id") or "") == supplied_id
        }
    )
    return {
        "accepted": False,
        "error": error,
        "question_id": supplied_id,
        "supplied_id_kind": "requirement_id" if owning_question_ids else "unknown",
        "replace_with_question_ids": owning_question_ids,
        "allowed_question_ids": question_ids,
        "output_language": str((workspace.get("intent") or {}).get("language") or ""),
        "next_action": (
            "Replace only question_id with one returned owning question_id and retry."
            if owning_question_ids
            else "Copy one exact question_id from set_overview and retry."
        ),
    }


def possible_question_ids_error(
    workspace: dict[str, Any],
    values: Iterable[str],
    *,
    finding_index: int,
) -> dict[str, Any] | None:
    """Explain question/requirement ID confusion without discarding meaning."""

    questions = [
        item
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict) and str(item.get("question_id") or "")
    ]
    question_ids = {str(item.get("question_id") or "") for item in questions}
    requested = list(dict.fromkeys(str(value) for value in values if str(value)))
    invalid = [value for value in requested if value not in question_ids]
    if not invalid:
        return None

    requirement_owners: dict[str, list[str]] = {}
    for question in questions:
        question_id = str(question.get("question_id") or "")
        for requirement in question.get("answer_requirements") or []:
            if not isinstance(requirement, dict):
                continue
            requirement_id = str(requirement.get("requirement_id") or "")
            if requirement_id:
                requirement_owners.setdefault(requirement_id, []).append(question_id)

    corrections = []
    for value in invalid:
        owners = list(dict.fromkeys(requirement_owners.get(value, [])))
        corrections.append(
            {
                "supplied_id": value,
                "supplied_id_kind": "requirement_id" if owners else "unknown",
                "replace_with_question_ids": owners,
            }
        )
    return {
        "accepted": False,
        "error": "possible_question_ids_must_use_question_ids_not_requirement_ids",
        "finding_index": finding_index,
        "invalid_possible_question_ids": invalid,
        "question_id_corrections": corrections,
        "allowed_question_ids": sorted(question_ids),
        "output_language": str(
            ((workspace.get("intent") or {}).get("language") or "")
        ),
        "retry_policy": {
            "preserve_all_findings": True,
            "preserve_all_other_fields": True,
            "replace_only_invalid_possible_question_ids": True,
        },
        "next_action": (
            "Retry the same complete source analysis. Replace only the invalid possible_question_ids using the "
            "returned owning question_id values; do not remove findings, evidence, or other question links."
        ),
    }


__all__ = ["known_question_id_error", "possible_question_ids_error"]
