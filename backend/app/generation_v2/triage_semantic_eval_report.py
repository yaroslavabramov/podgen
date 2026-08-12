from __future__ import annotations

from typing import Any

from app.generation_v2.contracts import V2ToolName


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
    expected_salient_facts: list[dict[str, Any]] | None = None,
    expected_gaps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    source_refs = {str(item) for item in source_set.get("source_refs") or [] if str(item)}
    source_analyses = [
        item
        for item in workspace.get("source_analyses") or []
        if isinstance(item, dict)
        and str(item.get("source_ref") or "") in source_refs
        and item.get("current") is not False
        and item.get("accepted") is not False
    ]
    source_mapping_mode = bool(source_analyses)
    hints = [
        item
        for item in workspace.get("coverage_hints") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    findings = [
        item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    issues: list[str] = []
    has_expectations = bool(
        expected_question_statuses
        or expected_requirement_statuses
        or expected_salient_facts
        or expected_gaps
    )
    if not has_expectations:
        issues.append("semantic eval requires at least one explicit expectation")
    question_report: dict[str, Any] = {}
    for question_id in question_ids:
        analysis_findings = [
            finding
            for analysis in source_analyses
            for finding in analysis.get("findings") or []
            if isinstance(finding, dict) and question_id in {str(item) for item in finding.get("question_ids") or []}
        ]
        analysis_gaps = [
            gap
            for analysis in source_analyses
            for gap in analysis.get("gaps") or []
            if isinstance(gap, dict) and str(gap.get("question_id") or "") == question_id
        ]
        question_hints = [
            hint for hint in hints if str(hint.get("question_id") or "") == question_id
        ]
        linked_findings = [
            finding
            for finding in findings
            for link in finding.get("question_links") or []
            if isinstance(link, dict) and str(link.get("question_id") or "") == question_id
        ]
        question_report[question_id] = {
            "hint_count": len(question_hints),
            "linked_finding_count": len(linked_findings),
            "hint_requirement_ids": sorted(
                {
                    str(requirement_id)
                    for hint in question_hints
                    for requirement_id in hint.get("requirement_ids") or []
                    if str(requirement_id)
                }
            ),
            "candidate_finding_ids": sorted(
                {
                    str(finding_id)
                    for hint in question_hints
                    for finding_id in hint.get("candidate_finding_ids") or []
                    if str(finding_id)
                }
            ),
            "candidate_evidence_ids": sorted(
                {
                    str(evidence_id)
                    for hint in question_hints
                    for evidence_id in hint.get("candidate_evidence_ids") or []
                    if str(evidence_id)
                }
            ),
            "source_analysis_finding_count": len(analysis_findings),
            "source_analysis_gap_count": len(analysis_gaps),
        }
        expected = expected_question_statuses.get(question_id)
        if expected in {"hinted", "mapped"} and not (
            question_hints or linked_findings or analysis_findings or analysis_gaps
        ):
            issues.append(f"question {question_id} expected mapped material, got no source analysis material")
        elif expected == "no_hint" and (question_hints or linked_findings or analysis_findings or analysis_gaps):
            issues.append(f"question {question_id} expected no mapped material")
    for (question_id, requirement_id), expected in expected_requirement_statuses.items():
        has_hint = requirement_id in set(
            question_report.get(question_id, {}).get("hint_requirement_ids", [])
        )
        if expected in {"hinted", "mapped"} and not has_hint:
            issues.append(f"requirement {question_id}/{requirement_id} expected mapped hint")
        elif expected == "no_hint" and has_hint:
            issues.append(f"requirement {question_id}/{requirement_id} expected no hint")

    finding_by_id = {
        str(item.get("finding_id") or ""): item
        for item in findings
        if str(item.get("finding_id") or "")
    }
    salient_fact_report: list[dict[str, Any]] = []
    for fact in expected_salient_facts or []:
        label = str(fact.get("label") or "salient_fact")
        question_id = str(fact.get("question_id") or "")
        requirement_id = str(fact.get("requirement_id") or "")
        source_ref = str(fact.get("source_ref") or "")
        required_term_groups = _required_term_groups(fact)
        candidate_records: list[list[str]] = []
        for analysis in source_analyses:
            if source_ref and str(analysis.get("source_ref") or "") != source_ref:
                continue
            for finding in analysis.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                if question_id and question_id not in {str(item) for item in finding.get("question_ids") or []}:
                    continue
                if not finding.get("evidence_ids"):
                    continue
                candidate_records.append(_source_analysis_finding_statement_text(finding))
        if not source_mapping_mode:
            for hint in hints:
                if question_id and str(hint.get("question_id") or "") != question_id:
                    continue
                if source_ref and str(hint.get("source_ref") or "") != source_ref:
                    continue
                hint_requirement_ids = {str(item) for item in hint.get("requirement_ids") or []}
                if requirement_id and requirement_id not in hint_requirement_ids:
                    continue
                hint_record = [
                    str(hint.get("observed_gap") or ""),
                    str(hint.get("conflict_signal") or ""),
                    *[str(item) for item in hint.get("notes") or []],
                ]
                for finding_id in hint.get("candidate_finding_ids") or []:
                    hint_record.extend(_finding_semantic_text(finding_by_id.get(str(finding_id))))
                candidate_records.append(hint_record)
            for finding in findings:
                if source_ref and str(finding.get("source_ref") or "") != source_ref:
                    continue
                for link in finding.get("question_links") or []:
                    if not isinstance(link, dict):
                        continue
                    if question_id and str(link.get("question_id") or "") != question_id:
                        continue
                    link_requirement_ids = {str(item) for item in link.get("requirement_ids") or []}
                    if requirement_id and requirement_id not in link_requirement_ids:
                        continue
                    candidate_records.append([*_finding_semantic_text(finding), str(link.get("rationale") or "")])
        record_missing_terms = [_missing_terms(record, required_term_groups) for record in candidate_records]
        missing_terms = min(record_missing_terms, key=len) if record_missing_terms else _missing_group_labels(
            required_term_groups
        )
        passed = bool(candidate_records) and any(not missing for missing in record_missing_terms)
        salient_fact_report.append(
            {
                "label": label,
                "question_id": question_id,
                "requirement_id": requirement_id,
                "source_ref": source_ref,
                "passed": passed,
                "missing_terms": missing_terms,
            }
        )
        if not passed:
            issues.append(
                f"salient fact {label} expected mapped finding/hint material"
                + (f"; missing terms: {', '.join(missing_terms)}" if missing_terms else "")
            )

    gap_report: list[dict[str, Any]] = []
    for expected_gap in expected_gaps or []:
        label = str(expected_gap.get("label") or "expected_gap")
        question_id = str(expected_gap.get("question_id") or "")
        source_ref = str(expected_gap.get("source_ref") or "")
        scope = str(expected_gap.get("scope") or "source").strip().casefold()
        set_level = scope == "set"
        required_term_groups = _required_term_groups(expected_gap)
        candidate_records: list[list[str]] = []
        matched_source_refs: list[str] = []
        for analysis in source_analyses:
            analysis_source_ref = str(analysis.get("source_ref") or "")
            if set_level:
                if analysis.get("accepted") is not True or str(analysis.get("status") or "") != "usable":
                    continue
            elif source_ref and analysis_source_ref != source_ref:
                continue
            for gap in analysis.get("gaps") or []:
                if not isinstance(gap, dict):
                    continue
                if question_id and str(gap.get("question_id") or "") != question_id:
                    continue
                candidate_records.append([str(gap.get("description") or "")])
                if analysis_source_ref:
                    matched_source_refs.append(analysis_source_ref)
        record_missing_terms = [_missing_terms(record, required_term_groups) for record in candidate_records]
        missing_terms = min(record_missing_terms, key=len) if record_missing_terms else _missing_group_labels(
            required_term_groups
        )
        passed = bool(candidate_records) and any(not missing for missing in record_missing_terms)
        gap_report.append(
            {
                "label": label,
                "question_id": question_id,
                "source_ref": source_ref,
                "scope": "set" if set_level else "source",
                "passed": passed,
                "missing_terms": missing_terms,
                "matched_source_refs": sorted(set(matched_source_refs)),
            }
        )
        if not passed:
            expected_scope = "set-level" if set_level else "source-specific"
            issues.append(
                f"gap {label} expected {expected_scope} gap material"
                + (f"; missing terms: {', '.join(missing_terms)}" if missing_terms else "")
            )

    read_calls = [str(trace.tool_name) for trace in traces if str(trace.tool_name) in _SOURCE_READING_TOOLS]
    if read_calls:
        issues.append("semantic-only eval reread sources: " + ", ".join(read_calls))
    legacy_outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    if legacy_outcomes:
        issues.append(f"source-mapping recall eval wrote legacy outcomes: {len(legacy_outcomes)}")
    finish_traces = [trace for trace in traces if str(trace.tool_name) == str(V2ToolName.FINISH_TRIAGE_SOURCE_SET)]
    accepted_finishes = [trace for trace in finish_traces if (trace.output_json or {}).get("accepted") is True]
    if source_mapping_mode:
        analysed_refs = {str(item.get("source_ref") or "") for item in source_analyses}
        missing_refs = sorted(source_refs - analysed_refs)
        if missing_refs:
            issues.append("source-mapping eval missing SourceAnalysis for: " + ", ".join(missing_refs))
    elif len(accepted_finishes) != 1:
        issues.append(f"expected one accepted finish, got {len(accepted_finishes)}")
    if str(source_set.get("status") or "") != "mapped" and not source_mapping_mode:
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
        "questions": question_report,
        "salient_facts": salient_fact_report,
        "expected_gaps": gap_report,
        "coverage_hint_count": len(hints),
        "source_analysis_count": len(source_analyses),
        "source_read_call_count": len(read_calls),
        "finish_attempt_count": len(finish_traces),
        "accepted_finish_count": len(accepted_finishes),
        "rejected_tool_count": len(rejected_tools),
        "rejected_tools": rejected_tools,
    }


def _finding_semantic_text(finding: dict[str, Any] | None) -> list[str]:
    if not isinstance(finding, dict):
        return []
    return [
        str(finding.get("statement") or ""),
        str(finding.get("scope") or ""),
        *[str(item) for item in finding.get("limitations") or []],
        *[
            str(item.get("snippet_preview") or "")
            for item in finding.get("evidence_previews") or []
            if isinstance(item, dict)
        ],
    ]


def _source_analysis_finding_statement_text(finding: dict[str, Any]) -> list[str]:
    return [
        str(finding.get("statement") or ""),
    ]


def _required_term_groups(expectation: dict[str, Any]) -> list[list[str]]:
    raw_groups = expectation.get("required_term_groups") or []
    groups = [
        [str(term).casefold() for term in group if str(term).strip()]
        for group in raw_groups
        if isinstance(group, list)
    ]
    groups = [group for group in groups if group]
    if groups:
        return groups
    return [
        [str(term).casefold()]
        for term in expectation.get("required_terms") or []
        if str(term).strip()
    ]


def _missing_group_labels(required_term_groups: list[list[str]]) -> list[str]:
    return [" / ".join(group) for group in required_term_groups]


def _missing_terms(record: list[str], required_term_groups: list[list[str]]) -> list[str]:
    text = "\n".join(item for item in record if item).casefold()
    return [
        " / ".join(group)
        for group in required_term_groups
        if not any(term in text for term in group)
    ]


__all__ = ["semantic_eval_report"]
