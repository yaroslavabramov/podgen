from __future__ import annotations

import json
from typing import Any


ACTIVE_COVERAGE_STATUSES = frozenset({"covered", "partial", "conflicted"})
FINAL_COVERAGE_STATUSES = ("covered", "partial", "missing", "conflicted", "deferred")


def coverage_synthesis_lineage(
    outcomes: list[dict[str, Any]],
    *,
    status: str,
    source_ref_by_id: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    """Derive final synthesis lineage from completed per-set outcomes."""

    selected = (
        [item for item in outcomes if str(item.get("status") or "") in ACTIVE_COVERAGE_STATUSES]
        if status in ACTIVE_COVERAGE_STATUSES
        else outcomes
    )
    source_refs: list[str] = []
    evidence_ids: list[str] = []
    outcome_refs: list[str] = []
    for outcome in selected:
        direct_refs = [str(item) for item in outcome.get("source_refs") or [] if str(item).strip()]
        if not direct_refs:
            direct_refs = [
                source_ref_by_id.get(str(source_id), "")
                for source_id in outcome.get("source_ids") or []
                if source_ref_by_id.get(str(source_id))
            ]
        source_refs.extend(direct_refs)
        evidence_ids.extend(
            str(item) for item in outcome.get("evidence_ids") or [] if str(item).strip()
        )
        outcome_id = str(outcome.get("outcome_id") or "").strip()
        if not outcome_id:
            question_id = str(outcome.get("question_id") or "").strip()
            set_id = str(outcome.get("set_id") or "").strip()
            outcome_id = f"outcome:{question_id}:{set_id}" if question_id and set_id else ""
        if outcome_id:
            outcome_refs.append(outcome_id)
    return (
        list(dict.fromkeys(source_refs)),
        list(dict.fromkeys(evidence_ids)),
        list(dict.fromkeys(outcome_refs)),
    )


def coverage_finalization_payload(
    workspace: dict[str, Any],
    *,
    model_summary: str,
) -> dict[str, Any]:
    """Build backend-owned source-triage finalization metadata."""

    from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash

    questions = [
        item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)
    ]
    syntheses = [
        item for item in workspace.get("coverage_syntheses") or [] if isinstance(item, dict)
    ]
    if not syntheses:
        summary = str(model_summary or "")
        return {
            "ready": True,
            "model_summary": summary,
            "summary": summary,
            "handoff_mode": "source_mapping",
            "coverage_question_count": len(questions),
            "source_finding_count": len(
                [item for item in workspace.get("source_findings") or [] if isinstance(item, dict)]
            ),
            "coverage_hint_count": len(
                [item for item in workspace.get("coverage_hints") or [] if isinstance(item, dict)]
            ),
        }
    question_ids = [str(item.get("question_id") or "").strip() for item in questions]
    synthesis_ids = [str(item.get("question_id") or "").strip() for item in syntheses]
    if (
        any(not question_id for question_id in question_ids)
        or len(set(question_ids)) != len(question_ids)
        or len(set(synthesis_ids)) != len(synthesis_ids)
        or set(synthesis_ids) != set(question_ids)
    ):
        raise ValueError("Coverage gap ledger requires exactly one synthesis for every coverage question.")

    synthesis_by_question = {
        str(item.get("question_id") or ""): item for item in syntheses
    }
    current_revision = int(workspace.get("triage_semantic_revision") or 0)
    status_counts = {status: 0 for status in FINAL_COVERAGE_STATUSES}
    gaps: list[dict[str, str]] = []
    for question in questions:
        question_id = str(question.get("question_id") or "")
        synthesis = synthesis_by_question[question_id]
        status = str(synthesis.get("status") or "")
        if status not in status_counts:
            raise ValueError(f"Coverage gap ledger received non-final status for {question_id!r}.")
        if (
            int(synthesis.get("input_revision") or 0) != current_revision
            or str(synthesis.get("input_hash") or "")
            != coverage_synthesis_input_hash(workspace, question_id)
        ):
            raise ValueError(f"Coverage gap ledger received stale synthesis for {question_id!r}.")
        status_counts[status] += 1
        if status == "covered":
            continue
        gaps.append(
            {
                "question_id": question_id,
                "question_text": str(question.get("question") or ""),
                "status": status,
                "missing_detail": str(synthesis.get("missing_detail") or ""),
                "conflict_detail": str(synthesis.get("conflict_summary") or ""),
                "deferred_detail": str(synthesis.get("deferred_reason") or ""),
                "next_action": str(synthesis.get("next_action") or ""),
            }
        )

    summary = str(model_summary or "")
    return {
        "ready": True,
        # Keep the model's prose separate from the backend-derived ledger.
        "model_summary": summary,
        # Compatibility alias for readers of completed workspaces created before
        # the explicit model_summary field was introduced.
        "summary": summary,
        "coverage_question_count": len(questions),
        "coverage_status_counts": status_counts,
        "coverage_gap_count": len(gaps),
        "coverage_gaps": gaps,
    }


def coverage_terminal_completion_payload(finalization: dict[str, Any]) -> dict[str, Any]:
    """Return a terminal payload whose gaps cannot be omitted by model prose."""

    if "coverage_gaps" not in finalization:
        # Completed old workspaces have no backend ledger. Preserve their
        # readable terminal behavior without pretending that counts exist.
        return {
            "ready": True,
            "summary": str(
                finalization.get("model_summary")
                or finalization.get("summary")
                or "Source map already finalized."
            ),
            "notes": [],
        }

    gaps = [item for item in finalization.get("coverage_gaps") or [] if isinstance(item, dict)]
    status_counts = {
        status: int((finalization.get("coverage_status_counts") or {}).get(status) or 0)
        for status in FINAL_COVERAGE_STATUSES
    }
    question_count = int(finalization.get("coverage_question_count") or 0)
    gap_count = int(finalization.get("coverage_gap_count") or 0)
    if gap_count != len(gaps) or question_count != sum(status_counts.values()):
        raise ValueError("Persisted coverage finalization counts do not match its gap ledger.")
    counts = ", ".join(f"{status}={status_counts[status]}" for status in FINAL_COVERAGE_STATUSES)
    return {
        "ready": True,
        "summary": (
            f"Source map finalized from {question_count} coverage questions: "
            f"{counts}; coverage_gaps={gap_count}."
        ),
        "notes": [
            "coverage_gap:" + json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in gaps
        ],
    }


__all__ = [
    "ACTIVE_COVERAGE_STATUSES",
    "FINAL_COVERAGE_STATUSES",
    "coverage_finalization_payload",
    "coverage_synthesis_lineage",
    "coverage_terminal_completion_payload",
]
