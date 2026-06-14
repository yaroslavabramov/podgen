from __future__ import annotations

import re
import uuid

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
)
from app.models.chunk import Chunk
from app.models.generation_v2 import SourceRepresentation, SourceRepresentationKind, V2RecordStatus
from app.services.storage import download_text, get_minio_client


MIN_CITATION_CONFIDENCE = 0.65
_FACTUAL_CLAIM_TYPES = {"factual", "quote", "causal", "statistical", "historical"}


async def validate_citations(
    db: AsyncSession | None,
    *,
    bundle: ClaimCitationBundle,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
) -> CitationValidationReport:
    claim_by_id = {claim.claim_id: claim for claim in bundle.claims}
    source_ids = {source.source_id for source in source_cards}
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

    for link in bundle.citation_links:
        links_by_claim.setdefault(link.claim_id, []).append(link)
        claim = claim_by_id.get(link.claim_id)
        act_number = claim.act_number if claim else 0
        if claim is None:
            invalid_link_count += 1
            issues.append(_issue(link.claim_id, act_number, "Citation references an unknown claim id.", "Remove the citation or relink it to an extracted claim.", severity="high"))
            continue
        if link.source_id not in source_ids:
            invalid_link_count += 1
            issues.append(_issue(claim.claim_id, act_number, "Citation references an unknown source id.", "Relink the claim to a known source card.", severity="high"))
        if not link.locator.strip():
            invalid_link_count += 1
            issues.append(_issue(claim.claim_id, act_number, "Citation locator is empty.", "Provide a section, page, chunk, provider locator, or quote-card locator.", severity="high"))
        if link.confidence < MIN_CITATION_CONFIDENCE:
            low_confidence_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    f"Citation confidence {link.confidence:.2f} is below {MIN_CITATION_CONFIDENCE:.2f}.",
                    "Find a stronger citation or rewrite the claim with uncertainty.",
                    severity="medium",
                )
            )
        if link.quote:
            quote_found = await _quote_found(db, link, quote_cards_by_source.get(link.source_id, []))
            if quote_found:
                quote_match_count += 1
            else:
                quote_mismatch_count += 1
                invalid_link_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        "Citation quote could not be found in quote cards, chunks, or source representations.",
                        "Use an exact source quote or remove the quoted support.",
                        severity="high",
                    )
                )
        if link.quote and not link.representation_id:
            matching_quote = _matching_quote_card(link, quote_cards_by_source.get(link.source_id, []))
            if matching_quote is not None and matching_quote.representation_id:
                invalid_link_count += 1
                issues.append(
                    _issue(
                        claim.claim_id,
                        act_number,
                        "Chunk-backed citation is missing representation_id.",
                        "Set representation_id from the matching quote card or relink to provider citation metadata.",
                        severity="medium",
                    )
                )

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
                    "Add a citation link or rewrite the line as interpretation/open question.",
                    severity="high",
                )
            )
        if required and claim.support_level == "unsupported":
            weak_required_claim_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    claim.act_number,
                    "Required factual claim is marked unsupported.",
                    "Remove the claim or support it with a reliable source.",
                    severity="high",
                )
            )
        elif required and claim.support_level == "weak":
            weak_required_claim_count += 1
            issues.append(
                _issue(
                    claim.claim_id,
                    claim.act_number,
                    "Required factual claim has only weak support.",
                    "Find stronger support or soften the claim.",
                    severity="high",
                )
            )
        elif required and claim.support_level == "supported":
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
    }


def _requires_citation(claim: DraftClaim) -> bool:
    return claim.source_need == "required" and claim.claim_type in _FACTUAL_CLAIM_TYPES


async def _quote_found(db: AsyncSession | None, link: CitationLink, quote_cards: list[QuoteCard]) -> bool:
    quote = link.quote or ""
    if not quote.strip():
        return True
    normalized_quote = _normalize(quote)
    for card in quote_cards:
        card_quote = _normalize(card.quote)
        if normalized_quote in card_quote or card_quote in normalized_quote:
            return True
    if db is None:
        return False
    try:
        source_id = uuid.UUID(link.source_id)
    except ValueError:
        return False
    snippet = _search_snippet(quote)
    if snippet:
        chunk_result = await db.execute(
            select(Chunk.id).where(Chunk.source_id == source_id, Chunk.content.ilike(f"%{snippet}%")).limit(1)
        )
        if chunk_result.scalar_one_or_none() is not None:
            return True
    rep_result = await db.execute(
        select(SourceRepresentation).where(
            SourceRepresentation.source_id == source_id,
            SourceRepresentation.status == V2RecordStatus.READY,
            SourceRepresentation.kind.in_([SourceRepresentationKind.EXTRACTED_TEXT, SourceRepresentationKind.TRANSCRIPT, SourceRepresentationKind.FETCHED_HTML, SourceRepresentationKind.RENDERED_SNAPSHOT]),
        )
    )
    for rep in rep_result.scalars():
        if not rep.storage_path:
            continue
        try:
            text = download_text(get_minio_client(), rep.storage_path)
        except Exception:
            continue
        if normalized_quote in _normalize(text):
            return True
    return False


def _matching_quote_card(link: CitationLink, quote_cards: list[QuoteCard]) -> QuoteCard | None:
    if not link.quote:
        return None
    normalized_quote = _normalize(link.quote)
    for card in quote_cards:
        card_quote = _normalize(card.quote)
        if normalized_quote in card_quote or card_quote in normalized_quote:
            return card
    return None


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


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _search_snippet(text: str) -> str:
    normalized = _normalize(text)
    if len(normalized) <= 120:
        return normalized
    return normalized[:120]
