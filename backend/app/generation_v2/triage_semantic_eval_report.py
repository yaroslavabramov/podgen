from __future__ import annotations

from typing import Any

from app.generation_v2.contracts import V2ToolName
from app.generation_v2.source_triage_question_state import question_outcome_is_current


_SOURCE_READING_TOOLS = {
    str(V2ToolName.INSPECT_SOURCE),
    str(V2ToolName.OPEN_SOURCE_FILE),
    str(V2ToolName.READ_SOURCE),
    str(V2ToolName.READ_SOURCE_WINDOW),
    str(V2ToolName.READ_SOURCE_WITH_MODEL),
    str(V2ToolName.SEARCH_SOURCE),
}


def semantic_eval_report(
    *,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    traces: list[Any],
    question_ids: list[str],
    expected_question_statuses: dict[str, str],
    expected_requirement_statuses: dict[tuple[str, str], str],
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    outcomes = {
        str(item.get("question_id") or ""): item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    }
    issues: list[str] = []
    outcome_report: dict[str, Any] = {}
    for question_id in question_ids:
        outcome = outcomes.get(question_id)
        if outcome is None:
            issues.append(f"question {question_id} has no outcome")
            continue
        current = question_outcome_is_current(
            outcome,
            workspace=workspace,
            source_set=source_set,
            question_id=question_id,
        )
        requirement_statuses = {
            str(item.get("requirement_id") or ""): str(item.get("status") or "")
            for item in outcome.get("requirement_results") or []
            if isinstance(item, dict)
        }
        outcome_report[question_id] = {
            "status": str(outcome.get("status") or ""),
            "current": current,
            "requirement_statuses": requirement_statuses,
            "orientation_summary": str(outcome.get("orientation_summary") or ""),
            "missing_detail": str(outcome.get("missing_detail") or ""),
        }
        if not current:
            issues.append(f"question {question_id} outcome is stale")
        expected = expected_question_statuses.get(question_id)
        if expected and str(outcome.get("status") or "") != expected:
            issues.append(
                f"question {question_id} expected status {expected}, got {outcome.get('status')}"
            )
    for (question_id, requirement_id), expected in expected_requirement_statuses.items():
        actual = (
            outcome_report.get(question_id, {})
            .get("requirement_statuses", {})
            .get(requirement_id)
        )
        if actual != expected:
            issues.append(
                f"requirement {question_id}/{requirement_id} expected status {expected}, got {actual}"
            )

    read_calls = [str(trace.tool_name) for trace in traces if str(trace.tool_name) in _SOURCE_READING_TOOLS]
    if read_calls:
        issues.append("semantic-only eval reread sources: " + ", ".join(read_calls))
    finish_traces = [
        trace for trace in traces if str(trace.tool_name) == str(V2ToolName.FINISH_TRIAGE_SOURCE_SET)
    ]
    accepted_finishes = [trace for trace in finish_traces if (trace.output_json or {}).get("accepted") is True]
    if len(accepted_finishes) != 1:
        issues.append(f"expected one accepted finish, got {len(accepted_finishes)}")
    if str(source_set.get("status") or "") != "mapped":
        issues.append(f"source set expected mapped status, got {source_set.get('status')}")
    rejected_tools = [
        {
            "tool_name": str(trace.tool_name),
            "error": str((trace.output_json or {}).get("error") or ""),
        }
        for trace in traces
        if (trace.output_json or {}).get("error")
    ]
    return {
        "passed": not issues,
        "issues": issues,
        "set_status": str(source_set.get("status") or ""),
        "outcomes": outcome_report,
        "source_read_call_count": len(read_calls),
        "finish_attempt_count": len(finish_traces),
        "accepted_finish_count": len(accepted_finishes),
        "rejected_tool_count": len(rejected_tools),
        "rejected_tools": rejected_tools,
    }


__all__ = ["semantic_eval_report"]
