"""Shared claim/evidence context loaders for planning prompts."""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim, ClaimEvidence
from app.models.evidence import EvidenceProvenance, EvidenceSpan
from app.models.source import Source


def summarize_planning_context(
    *,
    claims: list[Claim],
    claim_rows: list[tuple[ClaimEvidence, EvidenceSpan, Source]],
    provenances: list[EvidenceProvenance],
    question_context_map: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    question_context_map = question_context_map or {}
    claim_index: dict[str, dict] = {
        str(claim.id): {
            "id": str(claim.id),
            "type": claim.claim_type,
            "verification": claim.verification_level,
            "statement": claim.statement[:500],
            "supporting_evidence_count": 0,
            "illustrative_evidence_count": 0,
            "has_illustrative": False,
            "branch_ids": set(),
            "question_ids": set(),
            "intents": set(),
            "direct_support_count": 0,
            "contextual_support_count": 0,
            "source_ids": set(),
            "quality_hints": {
                "has_primary_support": False,
                "has_high_reliability_support": False,
                "temporary_only_support": True,
            },
        }
        for claim in claims
    }

    evidence_index: dict[str, dict] = {}
    provenance_by_evidence: dict[uuid.UUID, list[EvidenceProvenance]] = defaultdict(list)
    for provenance in provenances:
        provenance_by_evidence[provenance.evidence_id].append(provenance)

    for ce, evidence, source in claim_rows:
        claim_id = str(ce.claim_id)
        claim_summary = claim_index.get(claim_id)
        if claim_summary is None:
            continue

        evidence_id = str(evidence.id)
        canonical_source_id = str(source.is_duplicate_of or source.id)
        contexts: list[dict] = []
        direct_flags: set[str] = set()
        branch_ids: set[str] = set()
        question_ids: set[str] = set()
        intents: set[str] = set()

        for provenance in provenance_by_evidence.get(evidence.id, []):
            question_context = question_context_map.get(provenance.question_id, {})
            direct_or_contextual = str(provenance.direct_or_contextual or "").strip()
            context = {
                "branch_id": provenance.branch_id,
                "question_id": provenance.question_id,
                "question": question_context.get("question", ""),
                "intent": question_context.get("intent", ""),
                "asset_type": str(provenance.asset_type),
                "direct_or_contextual": direct_or_contextual,
            }
            contexts.append(context)
            branch_ids.add(provenance.branch_id)
            question_ids.add(provenance.question_id)
            if question_context.get("intent"):
                intents.add(str(question_context["intent"]))
            if direct_or_contextual:
                direct_flags.add(direct_or_contextual)

        if ce.supports_fact:
            claim_summary["supporting_evidence_count"] += 1
            claim_summary["source_ids"].add(canonical_source_id)
            claim_summary["branch_ids"].update(branch_ids)
            claim_summary["question_ids"].update(question_ids)
            claim_summary["intents"].update(intents)
            if "direct" in direct_flags:
                claim_summary["direct_support_count"] += 1
            if "contextual" in direct_flags:
                claim_summary["contextual_support_count"] += 1
            claim_summary["quality_hints"]["has_primary_support"] = (
                claim_summary["quality_hints"]["has_primary_support"]
                or source.authority_type == "primary"
            )
            claim_summary["quality_hints"]["has_high_reliability_support"] = (
                claim_summary["quality_hints"]["has_high_reliability_support"]
                or source.domain_reliability == "high"
            )
            claim_summary["quality_hints"]["temporary_only_support"] = (
                claim_summary["quality_hints"]["temporary_only_support"]
                and bool(source.is_temporary)
            )
        if ce.illustrative:
            claim_summary["illustrative_evidence_count"] += 1
            claim_summary["has_illustrative"] = True

        evidence_summary = evidence_index.setdefault(
            evidence_id,
            {
                "id": evidence_id,
                "asset_type": evidence.asset_type,
                "quote": evidence.verbatim_quote[:220],
                "narrative_score": evidence.narrative_value_score,
                "illustrative": False,
                "supports_fact": False,
                "contradicts_fact": False,
                "source_title": source.title or "",
                "source_reliability": source.domain_reliability,
                "authority_type": source.authority_type,
                "is_temporary": source.is_temporary,
                "branch_ids": set(),
                "question_ids": set(),
                "intents": set(),
                "direct_or_contextual": set(),
                "question_contexts": [],
                "supporting_claim_ids": set(),
                "illustrative_claim_ids": set(),
            },
        )
        evidence_summary["illustrative"] = evidence_summary["illustrative"] or bool(ce.illustrative)
        evidence_summary["supports_fact"] = evidence_summary["supports_fact"] or bool(ce.supports_fact)
        evidence_summary["contradicts_fact"] = evidence_summary["contradicts_fact"] or bool(ce.contradicts_fact)
        evidence_summary["branch_ids"].update(branch_ids)
        evidence_summary["question_ids"].update(question_ids)
        evidence_summary["intents"].update(intents)
        evidence_summary["direct_or_contextual"].update(direct_flags)
        evidence_summary["question_contexts"].extend(contexts)
        if ce.supports_fact:
            evidence_summary["supporting_claim_ids"].add(claim_id)
        if ce.illustrative:
            evidence_summary["illustrative_claim_ids"].add(claim_id)

    for claim_summary in claim_index.values():
        claim_summary["branch_ids"] = sorted(claim_summary["branch_ids"])
        claim_summary["question_ids"] = sorted(claim_summary["question_ids"])
        claim_summary["intents"] = sorted(claim_summary["intents"])
        claim_summary["source_diversity"] = len(claim_summary["source_ids"])
        if claim_summary["supporting_evidence_count"] == 0:
            claim_summary["quality_hints"]["temporary_only_support"] = False
        del claim_summary["source_ids"]

    for evidence_summary in evidence_index.values():
        evidence_summary["branch_ids"] = sorted(evidence_summary["branch_ids"])
        evidence_summary["question_ids"] = sorted(evidence_summary["question_ids"])
        evidence_summary["intents"] = sorted(evidence_summary["intents"])
        evidence_summary["direct_or_contextual"] = sorted(evidence_summary["direct_or_contextual"])
        evidence_summary["supporting_claim_ids"] = sorted(evidence_summary["supporting_claim_ids"])
        evidence_summary["illustrative_claim_ids"] = sorted(evidence_summary["illustrative_claim_ids"])
        evidence_summary["question_contexts"] = sorted(
            {
                (
                    item.get("branch_id"),
                    item.get("question_id"),
                    item.get("direct_or_contextual"),
                    item.get("asset_type"),
                ): item
                for item in evidence_summary["question_contexts"]
            }.values(),
            key=lambda item: (
                item.get("branch_id", ""),
                item.get("question_id", ""),
                item.get("asset_type", ""),
            ),
        )

    return claim_index, evidence_index


async def load_planning_context(
    *,
    db: AsyncSession,
    claim_ids: list[str],
    generation_run_id: str | None,
    question_context_map: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    if not claim_ids:
        return {}, {}

    claim_uuid_ids = [uuid.UUID(cid) for cid in claim_ids]
    claims_result = await db.execute(select(Claim).where(Claim.id.in_(claim_uuid_ids)))
    claims = list(claims_result.scalars())

    claim_rows_result = await db.execute(
        select(ClaimEvidence, EvidenceSpan, Source)
        .join(EvidenceSpan, ClaimEvidence.evidence_id == EvidenceSpan.id)
        .join(Source, EvidenceSpan.source_id == Source.id)
        .where(ClaimEvidence.claim_id.in_(claim_uuid_ids))
    )
    claim_rows = list(claim_rows_result.all())

    provenances: list[EvidenceProvenance] = []
    evidence_ids = sorted({evidence.id for _, evidence, _ in claim_rows})
    if evidence_ids and generation_run_id:
        provenance_result = await db.execute(
            select(EvidenceProvenance).where(
                EvidenceProvenance.evidence_id.in_(evidence_ids),
                EvidenceProvenance.generation_run_id == uuid.UUID(generation_run_id),
            )
        )
        provenances = list(provenance_result.scalars())

    return summarize_planning_context(
        claims=claims,
        claim_rows=claim_rows,
        provenances=provenances,
        question_context_map=question_context_map,
    )
