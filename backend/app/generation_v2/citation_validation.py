from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import (
    CitationLink,
    CitationValidationReport,
    ClaimCitationBundle,
    DraftClaim,
    FactIssue,
    QuoteCard,
    SourceCard,
    SupportJudgment,
)
from app.models.generation_v2 import EvidenceItem, EvidenceStrength, RunCorpusView


MIN_CITATION_CONFIDENCE = 0.65
_FACTUAL_CLAIM_TYPES = {"factual", "quote", "causal", "statistical", "historical"}
_BLOCKING_SUPPORT_LEVELS = {"unsupported", "overstrong", "needs_attribution"}


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
    claim_by_id = {claim.claim_id: claim for claim in bundle.claims}
    legacy_mode = evidence_records is None and run_view is None
    source_ids = set(run_view.selected_source_ids if run_view else [source.source_id for source in source_cards])
    source_version_ids = set(run_view.selected_source_version_ids if run_view else [])
    evidence_by_id = await _load_evidence_by_id(db, run_view, evidence_records)
    judgments_by_pair = {
        (judgment.claim_id, judgment.evidence_id): judgment
        for judgment in support_judgments or []
    }
    quote_cards_by_evidence = {quote.evidence_id: quote for quote in quote_cards if quote.evidence_id}
    quote_cards_by_source: dict[str, list[QuoteCard]] = {}
    for quote in quote_cards:
        quote_cards_by_source.setdefault(quote.source_id, []).append(quote)

    issues: list[FactIssue] = []
    links_by_claim: dict[str, list[CitationLink]] = {}
    quote_match_count = 0
    quote_mismatch_count = 0
    low_confidence_count = 0
    invalid_link_count = 0
    missing_required_link_count = 0
    weak_required_claim_count = 0
    supported_required_claim_count = 0
    unsupported_judgment_count = 0
    supported_judgment_claim_ids: set[str] = set()

    for link in bundle.citation_links:
        links_by_claim.setdefault(link.claim_id, []).append(link)
        claim = claim_by_id.get(link.claim_id)
        act_number = claim.act_number if claim else 0
        if claim is None:
            invalid_link_count += 1
            issues.append(
                _issue(
                    link.claim_id,
                    act_number,
                    "Citation references an unknown claim id.",
                    "Remove the citation or relink it to an extracted claim.",
                    severity="high",
                )
            )
            continue
        required = _requires_citation(claim)
        if link.source_id not in source_ids:
            invalid_link_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    "Citation references an unknown source id.",
                    "Relink the claim to a source selected for this generation run.",
                    severity="high",
                )
            )
        evidence = evidence_by_id.get(str(link.evidence_id or ""))
        if legacy_mode:
            if link.quote and _legacy_quote_supported(link, quote_cards_by_source.get(link.source_id, [])):
                quote_match_count += 1
            elif link.quote:
                quote_mismatch_count += 1
                invalid_link_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        "Citation quote could not be found in quote cards.",
                        "Use an exact source quote or remove the quoted support.",
                        severity="high",
                    )
                )
        elif not link.evidence_id:
            invalid_link_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    "Citation-sensitive claim link is missing evidence_id.",
                    "Search evidence through V2 evidence tools and relink the claim to a saved EvidenceItem.",
                    severity="high" if required else "medium",
                )
            )
        elif evidence is None:
            invalid_link_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    "Citation references an unknown evidence_id.",
                    "Relink the claim to an EvidenceItem saved for this run.",
                    severity="high",
                )
            )
        else:
            invalid_link_count += _validate_evidence_link(
                link=link,
                claim=claim,
                evidence=evidence,
                selected_source_ids=source_ids,
                selected_source_version_ids=source_version_ids,
                issues=issues,
            )
            if _weak_evidence_for_required_claim(claim, evidence):
                weak_required_claim_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        "Required factual claim is linked only to weak or source-level evidence.",
                        "Find an exact/snippet-level EvidenceItem or rewrite the claim as uncertainty.",
                        severity="high",
                    )
                )
            quote_ok = _quote_supported(link, evidence, quote_cards_by_evidence.get(str(link.evidence_id)))
            if link.quote and quote_ok:
                quote_match_count += 1
            elif link.quote:
                quote_mismatch_count += 1
                invalid_link_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        "Citation quote could not be found in the linked EvidenceItem or matching quote card.",
                        "Use an exact source quote from saved evidence or remove the quote.",
                        severity="high",
                    )
                )

        if link.confidence < MIN_CITATION_CONFIDENCE:
            low_confidence_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    f"Citation confidence {link.confidence:.2f} is below {MIN_CITATION_CONFIDENCE:.2f}.",
                    "Find a stronger citation or rewrite the claim with uncertainty.",
                    severity="medium" if required else "low",
                )
            )

        judgment = judgments_by_pair.get((claim.claim_id, str(link.evidence_id or "")))
        judgment_required = bool(not legacy_mode and required and link.evidence_id and evidence is not None)
        if judgment_required and judgment is None:
            unsupported_judgment_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    "Required factual claim is missing a saved support judgment for the linked EvidenceItem.",
                    "Run support judgment for this claim/evidence pair or relink the claim before finalization.",
                    severity="high",
                )
            )
        elif judgment:
            if not judgment.supported:
                unsupported_judgment_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        f"Support judge marked evidence as not supported: {judgment.rationale}",
                        judgment.required_repair or "Rewrite or relink this claim to stronger saved evidence.",
                        severity="high" if required else "medium",
                    )
                )
            elif judgment.support_level in _BLOCKING_SUPPORT_LEVELS or judgment.blocking:
                unsupported_judgment_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        f"Support judge marked evidence as {judgment.support_level}: {judgment.rationale}",
                        judgment.required_repair or "Rewrite or relink this claim to stronger saved evidence.",
                        severity="high" if required else "medium",
                    )
                )
            elif required and judgment.support_level == "weak":
                unsupported_judgment_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        f"Support judge found only weak support: {judgment.rationale}",
                        judgment.required_repair or "Find stronger saved evidence or soften the claim.",
                        severity="high",
                    )
                )
            elif required and judgment.supported and judgment.support_level == "supported":
                supported_judgment_claim_ids.add(claim.claim_id)

    for claim in bundle.claims:
        required = _requires_citation(claim)
        claim_links = links_by_claim.get(claim.claim_id, [])
        if required and not claim_links:
            missing_required_link_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    claim.act_number,
                    "Required factual claim has no citation link.",
                    "Add an evidence-backed citation link or rewrite the line as interpretation/open question.",
                    severity="high",
                )
            )
        if required and not legacy_mode and claim.claim_id in supported_judgment_claim_ids:
            supported_required_claim_count += 1
        elif required and legacy_mode and claim.support_level == "unsupported":
            weak_required_claim_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    claim.act_number,
                    "Required factual claim is marked unsupported.",
                    "Remove the claim or support it with reliable saved evidence.",
                    severity="high",
                )
            )
        elif required and legacy_mode and claim.support_level == "weak":
            weak_required_claim_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    claim.act_number,
                    "Required factual claim has only weak self-reported support.",
                    "Find stronger saved evidence or soften the claim.",
                    severity="high",
                )
            )
        elif required and legacy_mode and claim.support_level == "supported":
            supported_required_claim_count += 1

    deduped = _dedupe_issues([*bundle.fact_issues, *issues])
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
        invalid_link_count=invalid_link_count,
        quote_match_count=quote_match_count,
        quote_mismatch_count=quote_mismatch_count,
        low_confidence_link_count=low_confidence_count,
        nonblocking_low_issue_count=nonblocking_low_count,
        blocking_issue_count=blocking_count,
        support_judgment_count=len(support_judgments or []),
        unsupported_judgment_count=unsupported_judgment_count,
    )


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


async def _load_evidence_by_id(
    db: AsyncSession | None,
    run_view: RunCorpusView | None,
    evidence_records: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if evidence_records is not None:
        return {str(item.get("id")): item for item in evidence_records if item.get("id")}
    if db is None or run_view is None:
        return {}
    result = await db.execute(select(EvidenceItem).where(EvidenceItem.generation_run_id == run_view.generation_run_id))
    return {str(item.id): _evidence_to_dict(item) for item in result.scalars()}


def _validate_evidence_link(
    *,
    link: CitationLink,
    claim: DraftClaim,
    evidence: dict[str, Any],
    selected_source_ids: set[str],
    selected_source_version_ids: set[str],
    issues: list[FactIssue],
) -> int:
    invalid = 0
    if not _evidence_is_selected_or_cited(evidence):
        invalid += 1
        issues.append(
            _issue(
                claim.claim_id,
                claim.act_number,
                "Citation evidence was not explicitly selected before citation.",
                "Call select_evidence for the EvidenceItem before linking it to a claim.",
                severity="high",
            )
        )
    if str(evidence.get("source_id")) != link.source_id:
        invalid += 1
        issues.append(
            _issue(
                claim.claim_id,
                claim.act_number,
                "Citation source_id does not match the linked EvidenceItem source_id.",
                "Copy source_id from the EvidenceItem or relink the claim.",
                severity="high",
            )
        )
    evidence_version_id = str(evidence.get("source_version_id") or "")
    if link.source_version_id and evidence_version_id != link.source_version_id:
        invalid += 1
        issues.append(
            _issue(
                claim.claim_id,
                claim.act_number,
                "Citation source_version_id does not match the linked EvidenceItem.",
                "Copy source_version_id from the EvidenceItem or relink the claim.",
                severity="high",
            )
        )
    if not link.source_version_id:
        invalid += 1
        issues.append(
            _issue(
                claim.claim_id,
                claim.act_number,
                "Citation is missing source_version_id.",
                "Set source_version_id from the linked EvidenceItem.",
                severity="high",
            )
        )
    if selected_source_ids and str(evidence.get("source_id")) not in selected_source_ids:
        invalid += 1
        issues.append(
            _issue(
                claim.claim_id,
                claim.act_number,
                "Citation evidence is outside the run corpus view source set.",
                "Use evidence selected for this generation run.",
                severity="high",
            )
        )
    if selected_source_version_ids and evidence_version_id not in selected_source_version_ids:
        invalid += 1
        issues.append(
            _issue(
                claim.claim_id,
                claim.act_number,
                "Citation evidence is outside the run corpus view source-version set.",
                "Use a source version selected by the run corpus view.",
                severity="high",
            )
        )
    if not str(evidence.get("snippet_text") or "").strip():
        invalid += 1
        issues.append(
            _issue(
                claim.claim_id,
                claim.act_number,
                "Linked EvidenceItem has no saved snippet_text.",
                "Relink to evidence with a persisted snippet.",
                severity="high",
            )
        )
    return invalid


def _weak_evidence_for_required_claim(claim: DraftClaim, evidence: dict[str, Any]) -> bool:
    if not _requires_citation(claim):
        return False
    return str(evidence.get("evidence_strength") or "") in {EvidenceStrength.WEAK, EvidenceStrength.SOURCE_LEVEL}


def _evidence_is_selected_or_cited(evidence: dict[str, Any]) -> bool:
    return str((evidence.get("metadata") or {}).get("usage_status") or "") in {"selected", "cited"}


def _quote_supported(link: CitationLink, evidence: dict[str, Any], quote_card: QuoteCard | None) -> bool:
    if not link.quote:
        return True
    normalized_quote = _normalize(link.quote)
    text = _normalize(
        " ".join(
            [
                str(evidence.get("snippet_text") or ""),
                str(evidence.get("normalized_quote_text") or ""),
                quote_card.quote if quote_card else "",
            ]
        )
    )
    return bool(normalized_quote and (normalized_quote in text or _search_snippet(normalized_quote) in text))


def _legacy_quote_supported(link: CitationLink, quote_cards: list[QuoteCard]) -> bool:
    if not link.quote:
        return True
    normalized_quote = _normalize(link.quote)
    for card in quote_cards:
        card_quote = _normalize(card.quote)
        if normalized_quote in card_quote or card_quote in normalized_quote:
            return True
    return False


def _requires_citation(claim: DraftClaim) -> bool:
    return claim.source_need == "required" and claim.claim_type in _FACTUAL_CLAIM_TYPES


def _issue(claim_id: str, act_number: int, issue: str, repair: str, *, severity: str) -> FactIssue:
    return FactIssue(
        claim_id=claim_id,
        act_number=act_number,
        issue=issue,
        repair=repair,
        severity=severity,  # type: ignore[arg-type]
        blocking=severity in {"medium", "high"},
    )


def _dedupe_issues(issues: list[FactIssue]) -> list[FactIssue]:
    seen: set[tuple[str, str]] = set()
    deduped: list[FactIssue] = []
    for issue in issues:
        key = (issue.claim_id, issue.issue)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def _evidence_to_dict(item: EvidenceItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "generation_run_id": str(item.generation_run_id),
        "project_id": str(item.project_id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "provider": item.provider,
        "provider_vector_store_id": item.provider_vector_store_id,
        "provider_file_id": item.provider_file_id,
        "provider_vector_store_file_id": item.provider_vector_store_file_id,
        "response_id": item.response_id,
        "tool_call_id": item.tool_call_id,
        "retrieval_channel": item.retrieval_channel,
        "query_text": item.query_text,
        "purpose": item.purpose,
        "search_result_index": item.search_result_index,
        "score": item.score,
        "snippet_text": item.snippet_text,
        "normalized_quote_text": item.normalized_quote_text,
        "locator_text": item.locator_text,
        "locator_type": item.locator_type,
        "evidence_strength": item.evidence_strength,
        "snippet_hash": item.snippet_hash,
        "metadata": item.metadata_,
    }


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _search_snippet(text: str) -> str:
    normalized = _normalize(text)
    if len(normalized) <= 120:
        return normalized
    return normalized[:120]
