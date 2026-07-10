from __future__ import annotations

import re
from typing import Any

from app.generation_v2.citation_validation_payloads import evidence_to_dict
from app.generation_v2.schemas import CitationLink, DraftClaim, FactIssue, QuoteCard
from app.models.generation_v2 import EvidenceStrength

FACTUAL_CLAIM_TYPES = {"factual", "quote", "causal", "statistical", "historical"}
BLOCKING_SUPPORT_LEVELS = {"unsupported", "overstrong", "needs_attribution"}


def validate_evidence_link(
    *,
    link: CitationLink,
    claim: DraftClaim,
    evidence: dict[str, Any],
    selected_source_ids: set[str],
    selected_source_version_ids: set[str],
    issues: list[FactIssue],
) -> int:
    invalid = 0
    if not evidence_is_selected_or_cited(evidence):
        invalid += 1
        issues.append(
            issue(
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
            issue(
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
            issue(
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
            issue(
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
            issue(
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
            issue(
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
            issue(
                claim.claim_id,
                claim.act_number,
                "Linked EvidenceItem has no saved snippet_text.",
                "Relink to evidence with a persisted snippet.",
                severity="high",
            )
        )
    return invalid


def weak_evidence_for_required_claim(claim: DraftClaim, evidence: dict[str, Any]) -> bool:
    if not requires_citation(claim):
        return False
    return str(evidence.get("evidence_strength") or "") in {EvidenceStrength.WEAK, EvidenceStrength.SOURCE_LEVEL}


def evidence_is_selected_or_cited(evidence: dict[str, Any]) -> bool:
    return str((evidence.get("metadata") or {}).get("usage_status") or "") in {"selected", "cited"}


def quote_supported(link: CitationLink, evidence: dict[str, Any], quote_card: QuoteCard | None) -> bool:
    if not link.quote:
        return True
    normalized_quote = normalize(link.quote)
    text = normalize(
        " ".join(
            [
                str(evidence.get("snippet_text") or ""),
                str(evidence.get("normalized_quote_text") or ""),
                quote_card.quote if quote_card else "",
            ]
        )
    )
    return bool(normalized_quote and (normalized_quote in text or search_snippet(normalized_quote) in text))


def legacy_quote_supported(link: CitationLink, quote_cards: list[QuoteCard]) -> bool:
    if not link.quote:
        return True
    normalized_quote = normalize(link.quote)
    for card in quote_cards:
        card_quote = normalize(card.quote)
        if normalized_quote in card_quote or card_quote in normalized_quote:
            return True
    return False


def requires_citation(claim: DraftClaim) -> bool:
    return claim.source_need == "required" and claim.claim_type in FACTUAL_CLAIM_TYPES


def issue(claim_id: str, act_number: int, issue_text: str, repair: str, *, severity: str) -> FactIssue:
    return FactIssue(
        claim_id=claim_id,
        act_number=act_number,
        issue=issue_text,
        repair=repair,
        severity=severity,  # type: ignore[arg-type]
        blocking=severity in {"medium", "high"},
    )


def dedupe_issues(issues: list[FactIssue]) -> list[FactIssue]:
    seen: set[tuple[str, str]] = set()
    deduped: list[FactIssue] = []
    for item in issues:
        key = (item.claim_id, item.issue)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped



def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def search_snippet(text: str) -> str:
    normalized = normalize(text)
    if len(normalized) <= 120:
        return normalized
    return normalized[:120]

__all__ = [
    "evidence_to_dict",
]
