from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.generation_v2.citation_validation_helpers import (
    BLOCKING_SUPPORT_LEVELS as _BLOCKING_SUPPORT_LEVELS,
    issue as _issue,
    legacy_quote_supported as _legacy_quote_supported,
    quote_supported as _quote_supported,
    requires_citation as _requires_citation,
    validate_evidence_link as _validate_evidence_link,
    weak_evidence_for_required_claim as _weak_evidence_for_required_claim,
)
from app.generation_v2.schemas import (
    CitationLink,
    ClaimCitationBundle,
    FactIssue,
    QuoteCard,
    SupportJudgment,
)

MIN_CITATION_CONFIDENCE = 0.65


@dataclass
class CitationLinkCheckResult:
    issues: list[FactIssue] = field(default_factory=list)
    links_by_claim: dict[str, list[CitationLink]] = field(default_factory=dict)
    quote_match_count: int = 0
    quote_mismatch_count: int = 0
    low_confidence_count: int = 0
    invalid_link_count: int = 0
    weak_required_claim_count: int = 0
    unsupported_judgment_count: int = 0
    supported_judgment_claim_ids: set[str] = field(default_factory=set)


def validate_citation_links(
    *,
    bundle: ClaimCitationBundle,
    evidence_by_id: dict[str, dict[str, Any]],
    quote_cards: list[QuoteCard],
    source_ids: set[str],
    source_version_ids: set[str],
    judgments_by_pair: dict[tuple[str, str], SupportJudgment],
    legacy_mode: bool,
) -> CitationLinkCheckResult:
    claim_by_id = {claim.claim_id: claim for claim in bundle.claims}
    quote_cards_by_evidence = {quote.evidence_id: quote for quote in quote_cards if quote.evidence_id}
    quote_cards_by_source: dict[str, list[QuoteCard]] = {}
    for quote in quote_cards:
        quote_cards_by_source.setdefault(quote.source_id, []).append(quote)

    result = CitationLinkCheckResult()
    for link in bundle.citation_links:
        result.links_by_claim.setdefault(link.claim_id, []).append(link)
        claim = claim_by_id.get(link.claim_id)
        act_number = claim.act_number if claim else 0
        if claim is None:
            result.invalid_link_count += 1
            result.issues.append(
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
            result.invalid_link_count += 1
            result.issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    "Citation references an unknown source id.",
                    "Relink the claim to a source selected for this generation run.",
                    severity="high",
                )
            )
        evidence = evidence_by_id.get(str(link.evidence_id or ""))
        _validate_link_evidence(
            result,
            link=link,
            claim=claim,
            act_number=act_number,
            required=required,
            evidence=evidence,
            selected_source_ids=source_ids,
            selected_source_version_ids=source_version_ids,
            quote_cards_by_source=quote_cards_by_source,
            quote_cards_by_evidence=quote_cards_by_evidence,
            legacy_mode=legacy_mode,
        )
        _validate_link_confidence(result, link=link, claim_id=claim.claim_id, act_number=act_number, required=required)
        _validate_support_judgment(
            result,
            link=link,
            claim_id=claim.claim_id,
            act_number=act_number,
            required=required,
            evidence=evidence,
            judgment=judgments_by_pair.get((claim.claim_id, str(link.evidence_id or ""))),
            legacy_mode=legacy_mode,
        )
    return result


def _validate_link_evidence(
    result: CitationLinkCheckResult,
    *,
    link: CitationLink,
    claim,
    act_number: int,
    required: bool,
    evidence: dict[str, Any] | None,
    selected_source_ids: set[str],
    selected_source_version_ids: set[str],
    quote_cards_by_source: dict[str, list[QuoteCard]],
    quote_cards_by_evidence: dict[str, QuoteCard],
    legacy_mode: bool,
) -> None:
    if legacy_mode:
        if link.quote and _legacy_quote_supported(link, quote_cards_by_source.get(link.source_id, [])):
            result.quote_match_count += 1
        elif link.quote:
            result.quote_mismatch_count += 1
            result.invalid_link_count += 1
            result.issues.append(
                _issue(
                    claim.claim_id,
                    act_number,
                    "Citation quote could not be found in quote cards.",
                    "Use an exact source quote or remove the quoted support.",
                    severity="high",
                )
            )
        return
    if not link.evidence_id:
        result.invalid_link_count += 1
        result.issues.append(
            _issue(
                claim.claim_id,
                act_number,
                "Citation-sensitive claim link is missing evidence_id.",
                "Search evidence through V2 evidence tools and relink the claim to a saved EvidenceItem.",
                severity="high" if required else "medium",
            )
        )
        return
    if evidence is None:
        result.invalid_link_count += 1
        result.issues.append(
            _issue(
                claim.claim_id,
                act_number,
                "Citation references an unknown evidence_id.",
                "Relink the claim to an EvidenceItem saved for this run.",
                severity="high",
            )
        )
        return
    result.invalid_link_count += _validate_evidence_link(
        link=link,
        claim=claim,
        evidence=evidence,
        selected_source_ids=selected_source_ids,
        selected_source_version_ids=selected_source_version_ids,
        issues=result.issues,
    )
    if _weak_evidence_for_required_claim(claim, evidence):
        result.weak_required_claim_count += 1
        result.issues.append(
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
        result.quote_match_count += 1
    elif link.quote:
        result.quote_mismatch_count += 1
        result.invalid_link_count += 1
        result.issues.append(
            _issue(
                claim.claim_id,
                act_number,
                "Citation quote could not be found in the linked EvidenceItem or matching quote card.",
                "Use an exact source quote from saved evidence or remove the quote.",
                severity="high",
            )
        )


def _validate_link_confidence(
    result: CitationLinkCheckResult,
    *,
    link: CitationLink,
    claim_id: str,
    act_number: int,
    required: bool,
) -> None:
    if link.confidence >= MIN_CITATION_CONFIDENCE:
        return
    result.low_confidence_count += 1
    result.issues.append(
        _issue(
            claim_id,
            act_number,
            f"Citation confidence {link.confidence:.2f} is below {MIN_CITATION_CONFIDENCE:.2f}.",
            "Find a stronger citation or rewrite the claim with uncertainty.",
            severity="medium" if required else "low",
        )
    )


def _validate_support_judgment(
    result: CitationLinkCheckResult,
    *,
    link: CitationLink,
    claim_id: str,
    act_number: int,
    required: bool,
    evidence: dict[str, Any] | None,
    judgment: SupportJudgment | None,
    legacy_mode: bool,
) -> None:
    judgment_required = bool(not legacy_mode and required and link.evidence_id and evidence is not None)
    if judgment_required and judgment is None:
        result.unsupported_judgment_count += 1
        result.issues.append(
            _issue(
                claim_id,
                act_number,
                "Required factual claim is missing a saved support judgment for the linked EvidenceItem.",
                "Run support judgment for this claim/evidence pair or relink the claim before finalization.",
                severity="high",
            )
        )
    elif judgment:
        if not judgment.supported:
            result.unsupported_judgment_count += 1
            result.issues.append(
                _issue(
                    claim_id,
                    act_number,
                    f"Support judge marked evidence as not supported: {judgment.rationale}",
                    judgment.required_repair or "Rewrite or relink this claim to stronger saved evidence.",
                    severity="high" if required else "medium",
                )
            )
        elif judgment.support_level in _BLOCKING_SUPPORT_LEVELS or judgment.blocking:
            result.unsupported_judgment_count += 1
            result.issues.append(
                _issue(
                    claim_id,
                    act_number,
                    f"Support judge marked evidence as {judgment.support_level}: {judgment.rationale}",
                    judgment.required_repair or "Rewrite or relink this claim to stronger saved evidence.",
                    severity="high" if required else "medium",
                )
            )
        elif required and judgment.support_level == "weak":
            result.unsupported_judgment_count += 1
            result.issues.append(
                _issue(
                    claim_id,
                    act_number,
                    f"Support judge found only weak support: {judgment.rationale}",
                    judgment.required_repair or "Find stronger saved evidence or soften the claim.",
                    severity="high",
                )
            )
        elif required and judgment.supported and judgment.support_level == "supported":
            result.supported_judgment_claim_ids.add(claim_id)
