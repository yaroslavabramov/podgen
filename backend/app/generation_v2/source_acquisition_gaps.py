from __future__ import annotations

from typing import Any

from app.generation_v2.contracts.research import ResearchAuditSeverity, ResearchAuditTargetStep
from app.generation_v2.contracts.source import CorpusGapKind, SeverityLevel
from app.generation_v2.source_acquisition_gap_helpers import add_corpus_gap, functions_from_text as _functions_from_text
from app.generation_v2.schemas import CorpusGap, CorpusGapReport, ResearchAudit, SourceMap, SourceTriageGate
from app.models.generation_run import GenerationRun


def build_corpus_gap_report(
    run: GenerationRun,
    *,
    trigger_phase: str,
    round_number: int,
    source_map: SourceMap | None = None,
    triage_completion_summary: str | None = None,
    triage_ready: bool | None = None,
    triage_gate: SourceTriageGate | None = None,
    research_audit: ResearchAudit | None = None,
    treatment_gate: Any | None = None,
) -> CorpusGapReport:
    config = run.config or {}
    duration = int(config.get("duration_target") or 1800)
    gaps: list[CorpusGap] = []
    research_audit_non_acquisition_only = False

    def add_gap(
        kind: CorpusGapKind,
        description: str,
        *,
        functions: list[str] | None = None,
        source_ids: list[str] | None = None,
        severity: SeverityLevel = SeverityLevel.MEDIUM,
        origin: str = "",
    ) -> None:
        add_corpus_gap(
            gaps,
            trigger_phase=trigger_phase,
            kind=kind,
            description=description,
            functions=functions,
            source_ids=source_ids,
            severity=severity,
            origin=origin,
        )

    if triage_ready is False:
        add_gap(
            CorpusGapKind.TRIAGE_NOT_READY,
            triage_completion_summary or "Source triage reported that the corpus is not ready.",
            severity=SeverityLevel.HIGH,
            origin="source_triage_completion",
        )
    if source_map:
        for gap in source_map.gaps:
            add_gap(
                CorpusGapKind.SOURCE_MAP_GAP,
                gap,
                functions=_functions_from_text(gap),
                severity=SeverityLevel.MEDIUM,
                origin="source_map",
            )
        for contradiction in source_map.contradictions:
            add_gap(
                CorpusGapKind.SOURCE_MAP_GAP,
                f"Contradiction needs better coverage: {contradiction}",
                functions=["contradiction", "tension"],
                severity=SeverityLevel.MEDIUM,
                origin="source_map",
            )
    if triage_gate and not triage_gate.passed:
        for function in triage_gate.missing_function_evidence:
            add_gap(
                CorpusGapKind.MISSING_FUNCTION,
                f"No selected evidence for editorial function: {function}.",
                functions=[function],
                severity=SeverityLevel.HIGH if function != "top_evidence_by_function" else SeverityLevel.MEDIUM,
                origin="source_triage_gate",
            )
        for source_id in triage_gate.missing_source_card_evidence_source_ids:
            add_gap(
                CorpusGapKind.COVERAGE,
                f"Source card has no selected evidence for source_id {source_id}.",
                source_ids=[source_id],
                severity=SeverityLevel.MEDIUM,
                origin="source_triage_gate",
            )
        if triage_gate.invalid_source_ids or triage_gate.invalid_evidence_ids:
            add_gap(
                CorpusGapKind.COVERAGE,
                f"Source map contains invalid ids: sources={triage_gate.invalid_source_ids}, evidence={triage_gate.invalid_evidence_ids}.",
                severity=SeverityLevel.HIGH,
                origin="source_triage_gate",
            )
    if research_audit and not research_audit.passed:
        if research_audit.issues:
            acquisition_issues = [
                issue
                for issue in research_audit.issues
                if issue.target_step == ResearchAuditTargetStep.SOURCE_ACQUISITION
            ]
            research_audit_non_acquisition_only = not acquisition_issues
            for issue in acquisition_issues:
                description = issue.suggested_repair or issue.description
                add_gap(
                    CorpusGapKind.RESEARCH_AUDIT,
                    description,
                    functions=_functions_from_text(description),
                    severity=(
                        SeverityLevel.HIGH
                        if issue.severity in {ResearchAuditSeverity.BLOCKING, ResearchAuditSeverity.MAJOR}
                        else SeverityLevel.MEDIUM
                    ),
                    origin="research_audit",
                )
        else:
            for item in [*research_audit.missing_material, *research_audit.required_followups]:
                add_gap(
                    CorpusGapKind.RESEARCH_AUDIT,
                    item,
                    functions=_functions_from_text(item),
                    severity=SeverityLevel.HIGH,
                    origin="research_audit",
                )
    if treatment_gate and not getattr(treatment_gate, "passed", True):
        for item in [*getattr(treatment_gate, "weak_sections", []), *getattr(treatment_gate, "rewrite_instructions", [])]:
            add_gap(
                CorpusGapKind.TREATMENT,
                item,
                functions=_functions_from_text(item),
                severity=SeverityLevel.HIGH,
                origin="treatment_gate",
            )
    if duration >= 3600 and source_map and len(source_map.evidence_ids) < 12:
        add_gap(
            CorpusGapKind.DURATION,
            "Long-form target needs broader source density before drafting.",
            functions=["tension", "mechanism", "change", "impact"],
            severity=SeverityLevel.HIGH,
            origin="duration_gate",
        )
    if not gaps and not research_audit_non_acquisition_only:
        add_gap(
            CorpusGapKind.COVERAGE,
            "Corpus coverage is weak or unspecified; acquire stronger primary and source-backed material.",
            severity=SeverityLevel.MEDIUM,
        )

    required_functions = sorted({function for gap in gaps for function in gap.required_editorial_functions if function})
    source_count = len(source_map.source_cards) if source_map else 0
    evidence_count = len(source_map.evidence_ids) if source_map else 0
    should_acquire = bool(gaps)
    return CorpusGapReport(
        run_id=str(run.id),
        project_id=str(run.project_id),
        round_number=round_number,
        trigger_phase=trigger_phase,
        target_duration_seconds=duration,
        summary=(
            f"{len(gaps)} corpus gap(s) require source acquisition before continuing."
            if should_acquire
            else "Research audit failure does not require source acquisition."
        ),
        gaps=gaps,
        required_editorial_functions=required_functions,
        source_count=source_count,
        evidence_count=evidence_count,
        should_acquire=should_acquire,
    )
