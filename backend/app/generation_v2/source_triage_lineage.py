from __future__ import annotations

from typing import Any


ACTIVE_COVERAGE_STATUSES = frozenset({"covered", "partial", "conflicted"})


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


__all__ = ["ACTIVE_COVERAGE_STATUSES", "coverage_synthesis_lineage"]
