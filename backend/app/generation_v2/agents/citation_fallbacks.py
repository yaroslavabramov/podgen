from __future__ import annotations

from app.generation_v2.schemas import (
    ActDraft,
    CitationLink,
    ClaimCitationBundle,
    DraftClaim,
    FactIssue,
    QuoteCard,
    SourceCard,
    SupportJudgmentBundle,
)


def fallback_claims(
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    drafts: list[ActDraft],
) -> ClaimCitationBundle:
    claims: list[DraftClaim] = []
    links: list[CitationLink] = []
    issues: list[FactIssue] = []
    for draft in drafts:
        source_id = draft.used_source_ids[0] if draft.used_source_ids else (source_cards[0].source_id if source_cards else "")
        claim_id = f"act-{draft.act_number}-core"
        statement = draft.lines[0].text if draft.lines else draft.title
        quote = next((quote for quote in quote_cards if quote.source_id == source_id), None)
        claims.append(
            DraftClaim(
                claim_id=claim_id,
                statement=statement[:500],
                act_number=draft.act_number,
                line_index=0,
                text_span=statement[:240],
                claim_type="factual",
                source_need="required",
                risk_level="low" if source_id else "high",
                support_level="supported" if source_id else "unsupported",
                evidence_ids=[quote.evidence_id] if quote and quote.evidence_id else [],
            )
        )
        if source_id:
            links.append(
                CitationLink(
                    claim_id=claim_id,
                    source_id=source_id,
                    source_version_id=quote.source_version_id if quote else None,
                    evidence_id=quote.evidence_id if quote else None,
                    quote=quote.quote if quote else None,
                    locator=quote.locator if quote else "act-level source focus",
                    representation_id=quote.representation_id if quote else None,
                    quote_card_ref=quote.locator if quote else None,
                    supports_span=statement[:240],
                    confidence=0.7 if quote and quote.evidence_id else 0.55,
                )
            )
        else:
            issues.append(
                FactIssue(
                    claim_id=claim_id,
                    act_number=draft.act_number,
                    issue="No source is available for this act-level claim.",
                    repair="Add sources or rewrite as an open question.",
                    severity="high",
                    blocking=True,
                )
            )
    return ClaimCitationBundle(claims=claims, citation_links=links, fact_issues=issues)


def fallback_support_judgments(bundle: ClaimCitationBundle, evidence_by_id: dict[str, dict]) -> SupportJudgmentBundle:
    judgments = []
    claim_by_id = {claim.claim_id: claim for claim in bundle.claims}
    for link in bundle.citation_links:
        evidence = evidence_by_id.get(str(link.evidence_id or ""))
        claim = claim_by_id.get(link.claim_id)
        if not evidence or not claim:
            continue
        snippet = str(evidence.get("snippet_text") or "").lower()
        statement = claim.statement.lower()
        terms = [term for term in statement.split() if len(term) > 5]
        overlap = len([term for term in terms if term in snippet])
        supported = overlap >= max(1, min(3, len(terms) // 4))
        judgments.append(
            {
                "claim_id": claim.claim_id,
                "evidence_id": str(link.evidence_id),
                "supported": supported,
                "support_level": "supported" if supported else "weak",
                "rationale": "Debug fallback lexical overlap judgment.",
                "required_repair": "" if supported else "Verify with a stronger evidence snippet or soften the claim.",
                "blocking": not supported and claim.source_need == "required",
            }
        )
    return SupportJudgmentBundle.model_validate({"judgments": judgments})


__all__ = ["fallback_claims", "fallback_support_judgments"]
