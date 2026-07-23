from __future__ import annotations

import hashlib
import json
from typing import Any

from app.generation_v2.source_triage_model_views import (
    model_safe_coverage_outcome,
    model_safe_coverage_question,
    model_safe_source_finding,
)


def question_input_snapshot_id(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    question_id: str,
) -> str:
    """Identify the complete semantic input from which a question outcome is made."""

    question = next(
        (
            item
            for item in workspace.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
        ),
        {},
    )
    payload = {
        "question": model_safe_coverage_question(question) if question else {},
        "finding_catalog_snapshot_id": finding_catalog_snapshot_id(
            workspace,
            source_set,
        ),
        # A broader question must be reconsidered when a narrower child result
        # changes. This is a structural dependency only; the backend still does
        # not decide whether either semantic conclusion is correct.
        "dependent_question_results": dependent_question_results(
            workspace,
            source_set,
            question_id=question_id,
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def question_outcome_is_current(
    outcome: dict[str, Any] | None,
    *,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    question_id: str,
) -> bool:
    if not isinstance(outcome, dict) or bool(outcome.get("is_stale")):
        return False
    saved_snapshot_id = str(outcome.get("workbench_snapshot_id") or "")
    return bool(saved_snapshot_id) and saved_snapshot_id == question_input_snapshot_id(
        workspace,
        source_set,
        question_id=question_id,
    )


def finding_catalog_snapshot_id(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> str:
    """Identify the complete set-wide semantic finding catalog."""

    set_id = str(source_set.get("set_id") or "")
    findings = sorted(
        [
            _input_finding(item)
            for item in workspace.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ],
        key=lambda item: str(item.get("finding_id") or ""),
    )
    return hashlib.sha256(
        json.dumps(
            findings,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def dependent_question_results(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    question_id: str,
) -> list[dict[str, Any]]:
    """Return direct child results that structurally inform a broader question."""

    set_id = str(source_set.get("set_id") or "")
    set_question_ids = {str(value) for value in source_set.get("question_ids") or []}
    children = sorted(
        [
            item
            for item in workspace.get("coverage_questions") or []
            if isinstance(item, dict)
            and str(item.get("parent_question_id") or "") == question_id
            and str(item.get("question_id") or "") in set_question_ids
        ],
        key=lambda item: str(item.get("question_id") or ""),
    )
    outcomes_by_question = {
        str(item.get("question_id") or ""): item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    }
    return [
        {
            "question": model_safe_coverage_question(child),
            "outcome": _input_outcome(outcomes_by_question.get(child_id)),
            "outcome_is_current": question_outcome_is_current(
                outcomes_by_question.get(child_id),
                workspace=workspace,
                source_set=source_set,
                question_id=child_id,
            ),
        }
        for child in children
        if (child_id := str(child.get("question_id") or ""))
    ]


def _input_finding(finding: dict[str, Any]) -> dict[str, Any]:
    payload = model_safe_source_finding(finding)
    # These links are the output of question resolution, not its input. Keeping
    # them in the signature would invalidate a resolution by merely saving it.
    payload["question_links"] = []
    payload["source_relations"] = []
    return payload


def _input_outcome(outcome: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(outcome, dict):
        return None
    payload = model_safe_coverage_outcome(outcome, source_ref_by_id={})
    for field in (
        "updated_at",
        "semantic_revision",
        "is_stale",
        "workbench_snapshot_id",
    ):
        payload.pop(field, None)
    return payload


__all__ = [
    "dependent_question_results",
    "finding_catalog_snapshot_id",
    "question_input_snapshot_id",
    "question_outcome_is_current",
]
