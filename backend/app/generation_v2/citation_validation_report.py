from __future__ import annotations

from app.generation_v2.schemas import CitationValidationReport, ClaimCitationBundle


def merge_validation_issues(bundle: ClaimCitationBundle, report: CitationValidationReport) -> ClaimCitationBundle:
    return bundle.model_copy(update={"fact_issues": report.issues})


def validation_quality_metrics(report: CitationValidationReport) -> dict[str, int | bool]:
    return {
        "citation_validation_passed": report.passed,
        "citation_validation_blocking_issue_count": report.blocking_issue_count,
        "citation_validation_nonblocking_low_issue_count": report.nonblocking_low_issue_count,
        "citation_validation_invalid_link_count": report.invalid_link_count,
        "citation_validation_quote_mismatch_count": report.quote_mismatch_count,
        "citation_validation_low_confidence_link_count": report.low_confidence_link_count,
        "citation_validation_missing_required_link_count": report.missing_required_link_count,
        "citation_validation_support_judgment_count": report.support_judgment_count,
        "citation_validation_unsupported_judgment_count": report.unsupported_judgment_count,
    }


__all__ = ["merge_validation_issues", "validation_quality_metrics"]
