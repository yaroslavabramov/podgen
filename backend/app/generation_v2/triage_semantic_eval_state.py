from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.generation_v2.source_triage_repair_state import (
    reconcile_carried_triage_evidence,
    triage_workspace_evidence_ids,
)


_PLANNING_SET_FIELDS = (
    "set_id",
    "label",
    "purpose",
    "rationale",
    "priority",
    "set_kind",
    "source_refs",
    "repair",
)
_SET_SCOPED_COLLECTIONS = (
    "source_assessments",
    "source_findings",
    "source_read_receipts",
    "reading_notes",
    "source_structure_pages",
)


def build_semantic_eval_workspace(
    *,
    fresh_workspace: dict[str, Any],
    source_workspace: dict[str, Any],
    set_id: str,
    question_ids: list[str],
    evidence_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Clone post-reading semantics while discarding every previous decision."""

    target_set = _required_record(
        source_workspace.get("source_sets"),
        key="set_id",
        value=set_id,
        label="source set",
    )
    questions_by_id = {
        str(item.get("question_id") or ""): item
        for item in source_workspace.get("coverage_questions") or []
        if isinstance(item, dict) and str(item.get("question_id") or "")
    }
    selected_ids = list(dict.fromkeys(str(value) for value in question_ids if str(value)))
    missing_question_ids = [value for value in selected_ids if value not in questions_by_id]
    if not selected_ids or missing_question_ids:
        raise ValueError(
            "semantic eval requires known question_ids; "
            f"missing={missing_question_ids or '<none supplied>'}"
        )

    clean = deepcopy(fresh_workspace)
    clean.update(
        {
            "status": "planned",
            "triage_semantic_eval": {
                "mode": "frozen_post_reading_state",
                "set_id": set_id,
                "question_ids": list(selected_ids),
                "read_policy": "source reading and source-analysis writes are outside this eval",
            },
            "coverage_questions": [deepcopy(questions_by_id[value]) for value in selected_ids],
            "central_question_id": (
                str(source_workspace.get("central_question_id") or "")
                if str(source_workspace.get("central_question_id") or "") in selected_ids
                else selected_ids[0]
            ),
            "plan_summary": str(source_workspace.get("plan_summary") or ""),
            "source_capsules": deepcopy(source_workspace.get("source_capsules") or {}),
            "source_sets": [
                _semantic_eval_set(
                    target_set,
                    question_ids=selected_ids,
                    questions_by_id=questions_by_id,
                )
            ],
            "current_set": None,
            "processed_sets": [],
            "coverage_outcomes": [],
            "coverage_syntheses": [],
            "source_triage_semantic_review_deliveries": [],
            "source_triage_question_workbench_deliveries": [],
            "source_triage_finding_catalog_deliveries": [],
            "source_triage_set_semantic_reviews": {},
            "source_triage_set_semantic_self_checks": {},
            "source_triage_finalizer_reads": {},
            "triage_semantic_revision": 0,
            "mapped_sources": [],
            "deferred_sources": [],
            "open_questions": [],
            "repair_operation": None,
        }
    )
    for field in _SET_SCOPED_COLLECTIONS:
        clean[field] = [
            deepcopy(item)
            for item in source_workspace.get(field) or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ]

    # Question links and outcomes are decisions under test. Source analysis,
    # exact evidence, scopes and limitations are the frozen input.
    for finding in clean["source_findings"]:
        finding["question_links"] = []

    if evidence_id_map is not None:
        required_ids = set(triage_workspace_evidence_ids(clean))
        missing_evidence_ids = sorted(required_ids - set(evidence_id_map))
        if missing_evidence_ids:
            raise ValueError(
                "semantic eval evidence clone is incomplete: "
                + ", ".join(missing_evidence_ids)
            )
        clean = reconcile_carried_triage_evidence(
            clean,
            evidence_id_map=evidence_id_map,
            invalid_evidence_ids=set(),
        )
    return clean


def semantic_eval_evidence_ids(workspace: dict[str, Any]) -> list[str]:
    return triage_workspace_evidence_ids(workspace)


def _semantic_eval_set(
    source_set: dict[str, Any],
    *,
    question_ids: list[str],
    questions_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    clean = {
        field: deepcopy(source_set[field])
        for field in _PLANNING_SET_FIELDS
        if field in source_set
    }
    clean.update(
        {
            "status": "planned",
            "question_ids": list(question_ids),
            "planned_question_ids": list(question_ids),
            "cross_question_ids": [],
            # This harness tests complete question resolution, not the planner's
            # distribution of requirements across later sets.
            "question_assignments": [
                {
                    "question_id": question_id,
                    "requirement_ids": [
                        str(item.get("requirement_id") or "")
                        for item in questions_by_id[question_id].get("answer_requirements") or []
                        if isinstance(item, dict) and str(item.get("requirement_id") or "")
                    ],
                }
                for question_id in question_ids
            ],
        }
    )
    return clean


def _required_record(
    records: Any,
    *,
    key: str,
    value: str,
    label: str,
) -> dict[str, Any]:
    match = next(
        (
            item
            for item in records or []
            if isinstance(item, dict) and str(item.get(key) or "") == value
        ),
        None,
    )
    if match is None:
        raise ValueError(f"semantic eval {label} {value!r} was not found")
    return match


__all__ = ["build_semantic_eval_workspace", "semantic_eval_evidence_ids"]
