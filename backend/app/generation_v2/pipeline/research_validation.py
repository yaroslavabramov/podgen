from __future__ import annotations

from app.generation_v2.pipeline.research_coverage import (
    research_cluster_is_major,
    research_source_map_cluster_coverage_issues,
    research_story_coverage,
)
from app.generation_v2.schemas import ResearchAudit, ResearchAuditIssue, ResearchMemo, SourceMap
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_CITED, EVIDENCE_STATUS_SELECTED
from app.models.generation_v2 import EvidenceStrength, RunCorpusView


def dedupe_strings(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def research_memo_evidence_ids(memo: ResearchMemo) -> list[str]:
    return dedupe_strings(
        evidence_id
        for atom in memo.editorial_atoms
        for evidence_id in atom.evidence_ids
        if evidence_id
    )


def selected_evidence_ids_for_run(
    evidence_records: list[dict],
    run_view: RunCorpusView,
) -> set[str]:
    valid_source_ids = {str(source_id) for source_id in run_view.selected_source_ids}
    valid_version_ids = {str(version_id) for version_id in run_view.selected_source_version_ids}
    revision = int(getattr(run_view, "revision", 1) or 1)
    attempt = int(getattr(run_view, "attempt", 1) or 1)
    return {
        str(item.get("id"))
        for item in evidence_records
        if item.get("id")
        and int(item.get("corpus_revision") or 1) == revision
        and int(item.get("attempt") or 1) == attempt
        and (item.get("metadata") or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
        and str(item.get("source_id")) in valid_source_ids
        and str(item.get("source_version_id")) in valid_version_ids
    }


def exact_quote_evidence_ids_for_run(
    evidence_records: list[dict],
    run_view: RunCorpusView,
) -> set[str]:
    selected_ids = selected_evidence_ids_for_run(evidence_records, run_view)
    exact_ids: set[str] = set()
    for item in evidence_records:
        evidence_id = str(item.get("id") or "")
        if evidence_id not in selected_ids or str(item.get("evidence_strength") or "") != EvidenceStrength.EXACT:
            continue
        metadata = item.get("metadata") or {}
        if metadata.get("model_file_read") and metadata.get("exact_quote_verified") is not True:
            continue
        exact_ids.add(evidence_id)
    return exact_ids


def validate_research_memo_evidence(
    memo: ResearchMemo,
    *,
    evidence_records: list[dict],
    run_view: RunCorpusView,
    duration_seconds: int,
    source_map: SourceMap | None = None,
) -> ResearchAudit:
    valid_source_ids = {str(source_id) for source_id in run_view.selected_source_ids}
    valid_version_ids = {str(version_id) for version_id in run_view.selected_source_version_ids}
    evidence_by_id = {str(item.get("id")): item for item in evidence_records if item.get("id")}
    valid_evidence_ids = selected_evidence_ids_for_run(evidence_records, run_view)
    valid_quote_evidence_ids = exact_quote_evidence_ids_for_run(evidence_records, run_view)
    issues: list[str] = []
    audit_issues: list[ResearchAuditIssue] = []

    def add_issue(
        description: str,
        *,
        issue_type: str = "weak_grounding",
        target_step: str = "editorial_research",
        severity: str = "blocking",
    ) -> None:
        issues.append(description)
        audit_issues.append(
            ResearchAuditIssue(
                issue_type=issue_type,  # type: ignore[arg-type]
                severity=severity,  # type: ignore[arg-type]
                target_step=target_step,  # type: ignore[arg-type]
                description=description,
                suggested_repair=description,
            )
        )

    if memo.open_questions:
        add_issue(
            "Research memo has blocking unresolved research gaps: " + "; ".join(memo.open_questions[:5]),
            issue_type="blocking_gap",
            target_step="editorial_research",
        )
    for idx, atom in enumerate(memo.editorial_atoms, start=1):
        invalid_sources = [source_id for source_id in atom.source_ids if source_id not in valid_source_ids]
        if invalid_sources:
            add_issue(f"Atom {idx} references unknown source ids: {', '.join(invalid_sources)}.")
        for evidence_id in atom.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                add_issue(f"Atom {idx} references unknown evidence id: {evidence_id}.")
                continue
            if evidence_id not in valid_evidence_ids:
                metadata = evidence.get("metadata") or {}
                if metadata.get("usage_status") not in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}:
                    add_issue(f"Atom {idx} references evidence id that was not explicitly selected: {evidence_id}.")
                elif str(evidence.get("source_id")) not in valid_source_ids or str(evidence.get("source_version_id")) not in valid_version_ids:
                    add_issue(f"Atom {idx} references evidence id outside the run corpus view: {evidence_id}.")
                else:
                    add_issue(f"Atom {idx} references invalid evidence id: {evidence_id}.")
        for quote_ref in atom.quote_refs:
            if quote_ref not in atom.evidence_ids:
                add_issue(f"Atom {idx} quote ref is not attached as atom evidence: {quote_ref}.")
            if quote_ref not in evidence_by_id:
                add_issue(f"Atom {idx} references unknown quote evidence id: {quote_ref}.")
            elif quote_ref not in valid_evidence_ids:
                add_issue(f"Atom {idx} references quote evidence outside the current run corpus view: {quote_ref}.")
            elif quote_ref not in valid_quote_evidence_ids:
                add_issue(f"Atom {idx} quote evidence is not canonically verified as exact: {quote_ref}.")
        if not atom.evidence_ids:
            add_issue(f"Atom {idx} ({atom.kind}) has no evidence ids.")
        if atom.source_coverage_status == "weak" and atom.story_weight == "core":
            add_issue(f"Core atom {idx} ({atom.kind}) is marked weak.")
    if duration_seconds >= 3600:
        grounded_core = [
            atom
            for atom in memo.editorial_atoms
            if atom.story_weight == "core" and atom.evidence_ids and not [eid for eid in atom.evidence_ids if eid not in valid_evidence_ids]
        ]
        if len(grounded_core) < 4:
            add_issue(
                "Long-form V2 memo requires at least four grounded core editorial atoms.",
                issue_type="insufficient_material",
            )
    if duration_seconds >= 2700:
        coverage = research_story_coverage(memo, valid_evidence_ids)
        required = {
            "mechanism": "45+ minute memo requires a grounded mechanism atom.",
            "context": "45+ minute memo requires grounded context/setup material.",
            "change": "45+ minute memo requires a grounded change/turn atom.",
            "impact": "45+ minute memo requires grounded impact/stakes material.",
            "driver_tension": "45+ minute memo requires grounded driver or tension material.",
            "open_question_risk": "45+ minute memo requires an open question or explicit risk.",
        }
        for key, message in required.items():
            if not coverage.get(key):
                add_issue(message, issue_type="missing_story_function")
    if source_map is not None:
        cluster_issues = research_source_map_cluster_coverage_issues(memo, source_map)
        for issue in cluster_issues:
            add_issue(issue, issue_type="missing_story_function")
    score = 1.0 if not issues else max(0.0, 1.0 - len(issues) * 0.15)
    repair_targets = sorted({issue.target_step for issue in audit_issues if issue.target_step != "none"})
    return ResearchAudit(
        passed=not issues,
        score=score,
        verdict="Research memo evidence gate passed." if not issues else "Research memo evidence gate failed.",
        missing_material=issues,
        required_followups=issues,
        blocking=bool(issues),
        issues=audit_issues,
        repair_targets=repair_targets,  # type: ignore[arg-type]
    )



__all__ = [
    "dedupe_strings",
    "exact_quote_evidence_ids_for_run",
    "research_cluster_is_major",
    "research_memo_evidence_ids",
    "research_source_map_cluster_coverage_issues",
    "research_story_coverage",
    "selected_evidence_ids_for_run",
    "validate_research_memo_evidence",
]
