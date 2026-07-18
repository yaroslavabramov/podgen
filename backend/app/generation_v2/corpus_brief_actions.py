from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.generation_v2.contracts.source import CoverageNextAction, CoverageOutcomeStatus
from app.generation_v2.schemas_source import CorpusBrief, CoverageOutcome


@dataclass(frozen=True)
class CorpusBriefAcquisitionRequest:
    question_id: str
    question: str
    reason: str
    question_kind: str
    importance: str
    related_source_ids: tuple[str, ...]


def corpus_brief_acquisition_requests(
    brief: CorpusBrief | None,
) -> list[CorpusBriefAcquisitionRequest]:
    if brief is None:
        return []
    question_by_id = {item.question_id: item for item in brief.coverage_questions}
    finding_by_id = {item.finding_id: item for item in brief.source_findings}
    requests: list[CorpusBriefAcquisitionRequest] = []
    for outcome in brief.coverage_outcomes:
        acquisition_results = _requirement_results_for_action(
            outcome,
            CoverageNextAction.SOURCE_ACQUISITION,
        )
        if not _outcome_routes_to(outcome, CoverageNextAction.SOURCE_ACQUISITION):
            continue
        question = question_by_id.get(outcome.question_id)
        requests.append(
            CorpusBriefAcquisitionRequest(
                question_id=outcome.question_id,
                question=question.question if question is not None else outcome.question_id,
                reason=(
                    _requirement_action_reason(question, acquisition_results)
                    if acquisition_results
                    else coverage_outcome_reason(outcome)
                ),
                question_kind=str(question.kind) if question is not None else "other",
                importance=str(question.importance) if question is not None else "supporting",
                related_source_ids=tuple(
                    dict.fromkeys(
                        finding_by_id[finding_id].source_id
                        for result in acquisition_results
                        for finding_id in result.finding_ids
                        if finding_id in finding_by_id
                    )
                    if acquisition_results
                    else outcome.source_ids
                ),
            )
        )
    return requests


def coverage_outcome_reason(outcome: CoverageOutcome) -> str:
    status_order = {
        CoverageOutcomeStatus.COVERED: (
            outcome.orientation_summary,
            outcome.missing_detail,
            outcome.conflict_summary,
            outcome.deferred_reason,
        ),
        CoverageOutcomeStatus.PARTIAL: (
            outcome.missing_detail,
            outcome.orientation_summary,
            outcome.conflict_summary,
            outcome.deferred_reason,
        ),
        CoverageOutcomeStatus.MISSING: (
            outcome.missing_detail,
            outcome.conflict_summary,
            outcome.deferred_reason,
            outcome.orientation_summary,
        ),
        CoverageOutcomeStatus.CONFLICTED: (
            outcome.conflict_summary,
            outcome.missing_detail,
            outcome.orientation_summary,
            outcome.deferred_reason,
        ),
        CoverageOutcomeStatus.DEFERRED: (
            outcome.deferred_reason,
            outcome.missing_detail,
            outcome.conflict_summary,
            outcome.orientation_summary,
        ),
    }
    return next(
        (
            value
            for value in status_order.get(
                outcome.status,
                (
                    outcome.missing_detail,
                    outcome.conflict_summary,
                    outcome.deferred_reason,
                    outcome.orientation_summary,
                ),
            )
            if value
        ),
        "",
    )


def outcome_routes_to_research(outcome: CoverageOutcome) -> bool:
    return _outcome_routes_to(outcome, CoverageNextAction.RESEARCH)


def requirement_ids_routed_to_research(outcome: CoverageOutcome) -> list[str]:
    return [
        item.requirement_id
        for item in _requirement_results_for_action(outcome, CoverageNextAction.RESEARCH)
    ]


def _outcome_routes_to(outcome: CoverageOutcome, action: CoverageNextAction) -> bool:
    if outcome.requirement_results:
        return bool(_requirement_results_for_action(outcome, action))
    return outcome.next_action == action


def _requirement_results_for_action(
    outcome: CoverageOutcome,
    action: CoverageNextAction,
) -> list[Any]:
    return [item for item in outcome.requirement_results if item.next_action == action]


def _requirement_action_reason(question: Any | None, results: list[Any]) -> str:
    description_by_id = {
        item.requirement_id: item.description
        for item in (getattr(question, "answer_requirements", None) or [])
    }
    parts: list[str] = []
    for result in results:
        description = description_by_id.get(result.requirement_id, result.requirement_id)
        detail = next(
            (
                value
                for value in (
                    result.missing_detail,
                    result.conflict_summary,
                    result.deferred_reason,
                    result.summary,
                )
                if value
            ),
            "",
        )
        separator = " " if description.endswith((".", ":", "?", "!")) else ": "
        parts.append(f"{description}{separator}{detail}" if detail else description)
    return "\n".join(parts)


def triage_state_acquisition_requests(triage_state: Any) -> list[CorpusBriefAcquisitionRequest]:
    brief = getattr(triage_state, "corpus_brief", None)
    return corpus_brief_acquisition_requests(brief if isinstance(brief, CorpusBrief) else None)


def triage_state_ready_for_research(triage_state: Any) -> bool:
    completion = getattr(triage_state, "triage_completion", None)
    gate = getattr(triage_state, "triage_gate", None)
    return bool(
        getattr(completion, "ready", False)
        and getattr(gate, "passed", False)
        and not triage_state_acquisition_requests(triage_state)
    )


def acquisition_editorial_functions(question_kind: str) -> list[str]:
    return {
        "context": ["context"],
        "origin": ["context"],
        "mechanism": ["mechanism"],
        "cause": ["driver"],
        "change": ["change"],
        "impact": ["impact"],
        "exception": ["exception"],
        "verification": ["context"],
    }.get(str(question_kind), [])


__all__ = [
    "CorpusBriefAcquisitionRequest",
    "acquisition_editorial_functions",
    "corpus_brief_acquisition_requests",
    "coverage_outcome_reason",
    "outcome_routes_to_research",
    "requirement_ids_routed_to_research",
    "triage_state_acquisition_requests",
    "triage_state_ready_for_research",
]
