from __future__ import annotations

from app.generation_v2.citation_validation_helpers import issue as _issue
from app.generation_v2.citation_validation_helpers import requires_citation as _requires_citation
from app.generation_v2.schemas import CitationLink, ClaimCitationBundle, FactIssue


def validate_claim_requirements(
    *,
    bundle: ClaimCitationBundle,
    links_by_claim: dict[str, list[CitationLink]],
    legacy_mode: bool,
    supported_judgment_claim_ids: set[str],
    issues: list[FactIssue],
) -> tuple[int, int, int]:
    missing_required_link_count = 0
    weak_required_claim_count = 0
    supported_required_claim_count = 0
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
    return missing_required_link_count, weak_required_claim_count, supported_required_claim_count


__all__ = ["validate_claim_requirements"]
