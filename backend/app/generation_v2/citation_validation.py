from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.citation_validation_evidence import load_evidence_by_id as _load_evidence_by_id_impl
from app.generation_v2.citation_validation_claims import validate_claim_requirements as _validate_claim_requirements
from app.generation_v2.schemas import CitationValidationReport, ClaimCitationBundle, QuoteCard, SourceCard, SupportJudgment
from app.generation_v2.citation_validation_report import (
    merge_validation_issues,
    validation_quality_metrics,
)
from app.generation_v2.citation_validation_helpers import (
    FACTUAL_CLAIM_TYPES as _FACTUAL_CLAIM_TYPES,
    dedupe_issues as _dedupe_issues,
)
from app.generation_v2.citation_validation_link_checks import (
    MIN_CITATION_CONFIDENCE,
    validate_citation_links as _validate_citation_links,
)
from app.models.generation_v2 import RunCorpusView



async def validate_citations(
    db: AsyncSession | None,
    *,
    bundle: ClaimCitationBundle,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    evidence_records: list[dict[str, Any]] | None = None,
    support_judgments: list[SupportJudgment] | None = None,
    run_view: RunCorpusView | None = None,
) -> CitationValidationReport:
    legacy_mode = evidence_records is None and run_view is None
    source_ids = set(run_view.selected_source_ids if run_view else [source.source_id for source in source_cards])
    source_version_ids = set(run_view.selected_source_version_ids if run_view else [])
    evidence_by_id = await _load_evidence_by_id(db, run_view, evidence_records)
    judgments_by_pair = {
        (judgment.claim_id, judgment.evidence_id): judgment
        for judgment in support_judgments or []
    }
    link_result = _validate_citation_links(
        bundle=bundle,
        evidence_by_id=evidence_by_id,
        quote_cards=quote_cards,
        source_ids=source_ids,
        source_version_ids=source_version_ids,
        judgments_by_pair=judgments_by_pair,
        legacy_mode=legacy_mode,
    )

    (
        missing_required_link_count,
        final_weak_required_claim_count,
        supported_required_claim_count,
    ) = _validate_claim_requirements(
        bundle=bundle,
        links_by_claim=link_result.links_by_claim,
        legacy_mode=legacy_mode,
        supported_judgment_claim_ids=link_result.supported_judgment_claim_ids,
        issues=link_result.issues,
    )
    weak_required_claim_count = link_result.weak_required_claim_count + final_weak_required_claim_count

    deduped = _dedupe_issues([*bundle.fact_issues, *link_result.issues])
    blocking_count = len([issue for issue in deduped if issue.severity in {"medium", "high"} or issue.blocking])
    nonblocking_low_count = len([issue for issue in deduped if issue.severity == "low" and not issue.blocking])
    return CitationValidationReport(
        passed=blocking_count == 0,
        issues=deduped,
        checked_claim_count=len(bundle.claims),
        checked_link_count=len(bundle.citation_links),
        supported_required_claim_count=supported_required_claim_count,
        weak_required_claim_count=weak_required_claim_count,
        missing_required_link_count=missing_required_link_count,
        invalid_link_count=link_result.invalid_link_count,
        quote_match_count=link_result.quote_match_count,
        quote_mismatch_count=link_result.quote_mismatch_count,
        low_confidence_link_count=link_result.low_confidence_count,
        nonblocking_low_issue_count=nonblocking_low_count,
        blocking_issue_count=blocking_count,
        support_judgment_count=len(support_judgments or []),
        unsupported_judgment_count=link_result.unsupported_judgment_count,
    )


async def _load_evidence_by_id(
    db: AsyncSession | None,
    run_view: RunCorpusView | None,
    evidence_records: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    return await _load_evidence_by_id_impl(db, run_view, evidence_records)

__all__ = [
    "_FACTUAL_CLAIM_TYPES",
    "merge_validation_issues",
    "validation_quality_metrics",
    "_validate_claim_requirements",
    "MIN_CITATION_CONFIDENCE",
    "_validate_citation_links",
]
