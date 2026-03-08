"""Phase 4.2: Verifier — hybrid rule-based + LLM claim verification."""

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.claim import Claim, ClaimEvidence, VerificationLevel
from app.models.evidence import EvidenceProvenance, EvidenceSpan
from app.models.source import Source
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.state import PipelineState
from app.realtime.events import claim_verified, progress, step_completed, step_started
from app.realtime.publisher import publish_event, update_generation_progress
from app.services.llm import call_sonnet_structured
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

VERIFIER_SYSTEM = """You are a claim verification engine for a podcast pipeline.
Given a batch of claims with their supporting evidence, evaluate whether the evidence truly supports each claim.

Return a JSON array:
[{
  "claim_id": "uuid",
  "verification_level": "VERIFIED|OBSERVED|UNVERIFIED|DISPUTED",
  "reasoning": "brief explanation",
  "needs_research": false
}]

Rules:
- VERIFIED: evidence clearly and directly confirms the claim from 2+ independent canonical sources, or from 1 strong primary/high source with direct textual support
- OBSERVED: evidence supports but not conclusively (single source, temporary source, non-primary source, or partial semantic match)
- UNVERIFIED: evidence doesn't adequately support the claim
- DISPUTED: evidence contains contradictory information
- needs_research=true: claim is important but current evidence is insufficient — request more research

Evaluate semantic alignment, not just keyword overlap. Respect source metadata:
- temporary-only evidence cannot justify VERIFIED
- source authority is a signal, not an automatic proof
- for REPORT/OPINION, check that attribution and quote alignment are meaningful."""

VERIFY_BATCH_SIZE = 8
_ATTRIBUTION_PLACEHOLDERS = {
    "unknown",
    "unspecified expert",
    "unidentified expert",
    "anonymous source",
    "source",
    "observer",
    "неустановленный эксперт",
    "источник",
    "наблюдатель",
    "анонимный источник",
}


def _index_provenance_asset_types(provenances: list[EvidenceProvenance]) -> dict[uuid.UUID, list[str]]:
    asset_types_by_evidence: dict[uuid.UUID, set[str]] = {}
    for provenance in provenances:
        asset_types_by_evidence.setdefault(provenance.evidence_id, set()).add(str(provenance.asset_type).upper())
    return {
        evidence_id: sorted(asset_types)
        for evidence_id, asset_types in asset_types_by_evidence.items()
    }


def _index_provenance_pairs(provenances: list[EvidenceProvenance]) -> list[dict]:
    pair_assets: dict[tuple[str, str], set[str]] = {}
    for provenance in provenances:
        pair_assets.setdefault(
            (provenance.branch_id, provenance.question_id),
            set(),
        ).add(str(provenance.asset_type).upper())
    return [
        {
            "branch_id": branch_id,
            "question_id": question_id,
            "found_assets": sorted(found_assets),
        }
        for (branch_id, question_id), found_assets in sorted(pair_assets.items())
    ]


async def _load_claim_evidence(
    claim: Claim,
    db: AsyncSession,
    *,
    generation_run_id: str | None = None,
) -> dict:
    """Load supporting/counter evidence for a claim."""
    ce_result = await db.execute(
        select(ClaimEvidence, EvidenceSpan, Source)
        .join(EvidenceSpan, ClaimEvidence.evidence_id == EvidenceSpan.id)
        .join(Source, EvidenceSpan.source_id == Source.id)
        .where(ClaimEvidence.claim_id == claim.id)
    )
    rows = ce_result.all()

    supporting: list[dict] = []
    has_counter = False
    source_ids: set[str] = set()
    supporting_evidence_ids: list[uuid.UUID] = []

    for ce, ev, src in rows:
        if ce.contradicts_fact:
            has_counter = True
        if not ce.supports_fact:
            continue

        canonical_source_id = str(src.is_duplicate_of or src.id)
        source_ids.add(canonical_source_id)
        supporting_evidence_ids.append(ev.id)
        supporting.append(
            {
                "evidence_id": str(ev.id),
                "quote": ev.verbatim_quote[:300],
                "asset_type": ev.asset_type,
                "asset_types": [str(ev.asset_type).upper()],
                "canonical_asset_type": ev.asset_type,
                "source_title": src.title or "",
                "source_url": src.original_url or "",
                "source_type": src.source_type,
                "published_at": src.published_at.isoformat() if src.published_at else None,
                "source_reliability": src.domain_reliability,
                "authority_type": src.authority_type,
                "is_temporary": src.is_temporary,
                "source_id": str(src.id),
                "canonical_source_id": canonical_source_id,
            }
        )

    provenance_pairs: list[dict] = []
    found_assets: set[str] = set()
    if supporting_evidence_ids and generation_run_id:
        provenance_result = await db.execute(
            select(EvidenceProvenance).where(
                EvidenceProvenance.evidence_id.in_(supporting_evidence_ids),
                EvidenceProvenance.generation_run_id == uuid.UUID(generation_run_id),
            )
        )
        provenances = list(provenance_result.scalars())
        asset_types_by_evidence = _index_provenance_asset_types(provenances)
        provenance_pairs = _index_provenance_pairs(provenances)
        for item in supporting:
            evidence_id = uuid.UUID(item["evidence_id"])
            contextual_asset_types = asset_types_by_evidence.get(
                evidence_id,
                [str(item["canonical_asset_type"]).upper()],
            )
            item["asset_types"] = contextual_asset_types
            item["asset_type"] = contextual_asset_types[0].lower()
            found_assets.update(contextual_asset_types)
    else:
        for item in supporting:
            found_assets.update(item["asset_types"])

    return {
        "supporting": supporting,
        "has_counter": has_counter,
        "source_ids": source_ids,
        "all_supporting_temporary": bool(supporting) and all(item["is_temporary"] for item in supporting),
        "branch_ids": sorted({pair["branch_id"] for pair in provenance_pairs}),
        "question_ids": sorted({pair["question_id"] for pair in provenance_pairs}),
        "provenance_pairs": provenance_pairs,
        "found_assets": sorted(found_assets),
    }


async def verify_single_claim(claim: Claim, db: AsyncSession) -> VerificationLevel:
    """Verify a single claim using the hybrid approach."""
    evidence_data = await _load_claim_evidence(claim, db)
    prefiltered = _prefilter_claim(claim, evidence_data)
    if prefiltered is not None:
        return prefiltered

    result = await _llm_verify_batch(
        [
            {
                "claim_id": str(claim.id),
                "statement": claim.statement[:500],
                "evidence": evidence_data["supporting"],
                "independent_source_count": len(evidence_data["source_ids"]),
            }
        ]
    )
    if result:
        level = _parse_verification_level(result[0].get("verification_level", "UNVERIFIED"))
        return _apply_verification_ceiling(level, evidence_data)

    return _rule_based_fallback(evidence_data)


def _prefilter_claim(claim: Claim, evidence_data: dict) -> VerificationLevel | None:
    if evidence_data["has_counter"]:
        return VerificationLevel.DISPUTED
    if not evidence_data["supporting"]:
        return VerificationLevel.UNVERIFIED
    if claim.claim_type in ("opinion", "report"):
        return VerificationLevel.OBSERVED if _is_meaningful_attribution(claim.attribution) else VerificationLevel.UNVERIFIED
    return None


async def _llm_verify_batch(claims_payload: list[dict]) -> list[dict]:
    prompt = json.dumps({"claims": claims_payload}, ensure_ascii=False)
    try:
        raw = await call_sonnet_structured(
            system=VERIFIER_SYSTEM,
            prompt=prompt,
            max_tokens=4096,
            temperature=0.2,
        )
        parsed = parse_llm_json(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "results" in parsed:
            return parsed["results"]
        return [parsed] if isinstance(parsed, dict) else []
    except Exception:
        logger.warning("LLM verification failed, falling back to rule-based", exc_info=True)
        return []


def _parse_verification_level(level_str: str) -> VerificationLevel:
    mapping = {
        "VERIFIED": VerificationLevel.VERIFIED,
        "OBSERVED": VerificationLevel.OBSERVED,
        "UNVERIFIED": VerificationLevel.UNVERIFIED,
        "DISPUTED": VerificationLevel.DISPUTED,
    }
    return mapping.get(level_str.upper(), VerificationLevel.UNVERIFIED)


def _has_strong_primary_support(supporting: list[dict]) -> bool:
    return any(
        item["authority_type"] == "primary"
        and item["source_reliability"] == "high"
        and not item["is_temporary"]
        for item in supporting
    )


def _apply_verification_ceiling(level: VerificationLevel, evidence_data: dict) -> VerificationLevel:
    if level != VerificationLevel.VERIFIED:
        return level
    if evidence_data["all_supporting_temporary"]:
        return VerificationLevel.OBSERVED
    if len(evidence_data["source_ids"]) >= 2:
        return level
    if _has_strong_primary_support(evidence_data["supporting"]):
        return level
    return VerificationLevel.OBSERVED


def _rule_based_fallback(evidence_data: dict) -> VerificationLevel:
    if evidence_data["has_counter"]:
        return VerificationLevel.DISPUTED
    if not evidence_data["supporting"]:
        return VerificationLevel.UNVERIFIED
    if evidence_data["all_supporting_temporary"]:
        return VerificationLevel.OBSERVED
    if len(evidence_data["source_ids"]) >= 2:
        return VerificationLevel.VERIFIED
    if _has_strong_primary_support(evidence_data["supporting"]):
        return VerificationLevel.OBSERVED
    if len(evidence_data["source_ids"]) == 1:
        return VerificationLevel.OBSERVED
    return VerificationLevel.UNVERIFIED


def _is_meaningful_attribution(attribution: dict | None) -> bool:
    if not isinstance(attribution, dict):
        return False
    who = attribution.get("who")
    if not isinstance(who, str):
        return False
    normalized = who.strip().lower()
    if not normalized or normalized in _ATTRIBUTION_PLACEHOLDERS:
        return False
    return len(normalized) >= 3


def _build_research_gaps(needs_research_claims: list[tuple[Claim, dict]]) -> list[dict]:
    gaps: dict[tuple[str, str], dict] = {}
    for claim, evidence_data in needs_research_claims:
        pair_contexts = evidence_data.get("provenance_pairs") or [
            {
                "branch_id": "verify",
                "question_id": f"verify:{claim.id}",
                "found_assets": evidence_data.get("found_assets", []),
            }
        ]
        for pair in pair_contexts:
            branch_id = pair["branch_id"]
            question_id = pair["question_id"]
            key = (branch_id, question_id)
            if key not in gaps:
                gaps[key] = {
                    "question_id": question_id,
                    "branch_id": branch_id,
                    "question": claim.statement,
                    "priority": 1,
                    "missing_assets": [],
                    "required_assets": [],
                    "expected_assets": [],
                    "found_assets": list(pair.get("found_assets", evidence_data.get("found_assets", []))),
                    "claim_id": str(claim.id),
                }
                continue
            merged_assets = set(gaps[key].get("found_assets", []))
            merged_assets.update(pair.get("found_assets", evidence_data.get("found_assets", [])))
            gaps[key]["found_assets"] = sorted(merged_assets)
    return sorted(gaps.values(), key=lambda gap: (gap["branch_id"], gap["question_id"]))


async def verify_claims(state: PipelineState) -> dict:
    """LangGraph node: verify claims using hybrid rule-based + LLM approach."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase4_claims", "verify", "Verifying claims"))
    update_generation_progress(run_id, "phase4_claims", "verifier", 43.0)

    claim_ids = state.get("claim_ids", [])
    if not claim_ids:
        return {"claim_ids": claim_ids}

    needs_research_claims: list[tuple[Claim, dict]] = []

    async with async_session_factory() as db:
        claims_for_llm: list[tuple[Claim, dict]] = []

        for cid in claim_ids:
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            claim = result.scalar_one_or_none()
            if not claim:
                continue

            evidence_data = await _load_claim_evidence(
                claim,
                db,
                generation_run_id=run_id,
            )
            prefiltered = _prefilter_claim(claim, evidence_data)
            if prefiltered is not None:
                claim.verification_level = prefiltered
                if claim.claim_type == "fact" and prefiltered == VerificationLevel.UNVERIFIED:
                    needs_research_claims.append((claim, evidence_data))
                publish_event(run_id, claim_verified(cid, prefiltered))
                continue

            claims_for_llm.append((claim, evidence_data))

        for batch_start in range(0, len(claims_for_llm), VERIFY_BATCH_SIZE):
            batch = claims_for_llm[batch_start:batch_start + VERIFY_BATCH_SIZE]
            batch_payload = [
                {
                    "claim_id": str(claim.id),
                    "statement": claim.statement[:500],
                    "evidence": ev_data["supporting"],
                    "independent_source_count": len(ev_data["source_ids"]),
                }
                for claim, ev_data in batch
            ]

            llm_results = await _llm_verify_batch(batch_payload)
            result_map = {
                r["claim_id"]: r
                for r in llm_results
                if isinstance(r, dict) and "claim_id" in r
            }

            for claim, ev_data in batch:
                cid = str(claim.id)
                llm_result = result_map.get(cid)

                if llm_result:
                    level = _parse_verification_level(llm_result.get("verification_level", "UNVERIFIED"))
                    level = _apply_verification_ceiling(level, ev_data)
                    if llm_result.get("needs_research") or level == VerificationLevel.UNVERIFIED:
                        needs_research_claims.append((claim, ev_data))
                else:
                    level = _rule_based_fallback(ev_data)
                    if level == VerificationLevel.UNVERIFIED:
                        needs_research_claims.append((claim, ev_data))

                claim.verification_level = level
                publish_event(run_id, claim_verified(cid, level))

        await db.commit()

    publish_event(run_id, step_completed("phase4_claims", "verify"))
    publish_event(run_id, progress(45.0, f"Verified {len(claim_ids)} claims"))

    iteration_counts = dict(state.get("iteration_counts", {}))
    verify_loops = iteration_counts.get("verify_research_loops", 0)
    needs_research = bool(needs_research_claims) and verify_loops < settings.max_verifier_research_loops
    if needs_research:
        iteration_counts["verify_research_loops"] = verify_loops + 1
    iteration_counts["verify_needs_research"] = needs_research

    return {
        "claim_ids": claim_ids,
        "current_phase": "phase4_claims",
        "iteration_counts": iteration_counts,
        "research_gaps": _build_research_gaps(needs_research_claims) if needs_research else [],
    }
