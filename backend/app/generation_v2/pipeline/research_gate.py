from __future__ import annotations

from typing import Any

from app.generation_v2.schemas import EpisodeCritique, ResearchAudit, ResearchAuditIssue

SOURCE_ACQUISITION_GAP_MARKERS = (
    "source_acquisition",
    "source acquisition",
    "missing source",
    "new source",
    "additional source",
    "outside corpus",
    "corpus lacks",
    "need source",
    "needs source",
)


def research_audit_target_steps(audit: ResearchAudit) -> set[str]:
    targets = {
        str(issue.target_step)
        for issue in audit.issues
        if str(issue.target_step or "none") != "none"
    }
    if targets:
        return targets
    return {str(target) for target in audit.repair_targets if str(target or "none") != "none"}


def research_audit_requires_source_acquisition(audit: ResearchAudit) -> bool:
    return "source_acquisition" in research_audit_target_steps(audit)


def gap_needs_source_acquisition(gap: Any) -> bool:
    if not isinstance(gap, dict):
        return False
    if str(gap.get("status") or "open") not in {"open", "deferred"}:
        return False
    severity = str(gap.get("severity") or "")
    treatment_relevance = str(gap.get("treatment_relevance") or "")
    if severity != "blocking" and treatment_relevance != "central":
        return False
    category = str(gap.get("category") or "")
    if category == "missing_source":
        return True
    text = " ".join(
        str(gap.get(key) or "")
        for key in ("question", "resolution_note", "category")
    ).lower()
    return any(marker in text for marker in SOURCE_ACQUISITION_GAP_MARKERS)


def source_acquisition_audit_from_research_gaps(workspace: dict[str, Any] | None) -> ResearchAudit | None:
    gaps = [gap for gap in (workspace or {}).get("research_gaps") or [] if gap_needs_source_acquisition(gap)]
    if not gaps:
        return None
    issues = [
        ResearchAuditIssue(
            issue_type="source_acquisition_needed",
            severity="blocking",
            target_step="source_acquisition",
            description=str(gap.get("question") or "Research repair needs source acquisition."),
            suggested_repair=str(
                gap.get("resolution_note")
                or gap.get("question")
                or "Acquire additional sources for this central research gap."
            ),
            source_refs=[str(item) for item in gap.get("source_refs") or [] if str(item).strip()],
        )
        for gap in gaps[:6]
    ]
    return ResearchAudit(
        passed=False,
        score=0.0,
        verdict="Central research gap requires source acquisition.",
        missing_material=[issue.description for issue in issues],
        required_followups=[issue.suggested_repair for issue in issues if issue.suggested_repair],
        blocking=True,
        issues=issues,
        repair_targets=["source_acquisition"],
    )


def research_audit_failure_message(audit: ResearchAudit, *, label: str = "V2 research audit failed") -> str:
    targets = sorted(research_audit_target_steps(audit))
    issues = [issue.description for issue in audit.issues[:5]]
    followups = issues or audit.required_followups[:5] or audit.missing_material[:5]
    suffix = f"; targets={targets}" if targets else ""
    if followups:
        suffix += "; followups=" + " | ".join(followups)
    return f"{label}: {audit.verdict}{suffix}"


def treatment_gate_research_issues(treatment_gate: EpisodeCritique) -> list[str]:
    research_markers = (
        "planning gap requires research repair",
        "partial/weak material",
    )
    issues: list[str] = []
    for issue in treatment_gate.weak_sections:
        text = str(issue or "").strip()
        if text and any(marker in text for marker in research_markers):
            issues.append(text)
    return issues[:4]


def treatment_repair_gap_id(issue: str, idx: int) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in issue).strip("_")
    slug = "_".join(part for part in slug.split("_") if part)[:64]
    return f"treatment_repair_{slug or idx}"
