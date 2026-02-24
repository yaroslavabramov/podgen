"""Phase 4.2: Verifier — hybrid rule-based + LLM claim verification."""

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.claim import Claim, ClaimEvidence, VerificationLevel
from app.models.evidence import EvidenceSpan
from app.models.source import Source
from app.pipeline.state import PipelineState
from app.pipeline.llm_json import parse_llm_json
from app.config import settings
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import step_started, step_completed, claim_verified, progress

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
- VERIFIED: evidence clearly and directly confirms the claim from 2+ independent reliable sources, or 1 authoritative primary source
- OBSERVED: evidence supports but not conclusively (single non-authoritative source, or weak semantic match)
- UNVERIFIED: evidence doesn't adequately support the claim
- DISPUTED: evidence contains contradictory information
- needs_research=true: claim is important but current evidence is insufficient — request more research

Evaluate semantic alignment, not just keyword overlap. A quote about "revenue growth" does not verify a claim about "profit margins"."""

# Batch size for LLM verification calls
VERIFY_BATCH_SIZE = 8


async def _load_claim_evidence(claim: Claim, db: AsyncSession) -> dict:
    """Load supporting/counter evidence for a claim. Returns structured data."""
    ce_result = await db.execute(
        select(ClaimEvidence, EvidenceSpan, Source)
        .join(EvidenceSpan, ClaimEvidence.evidence_id == EvidenceSpan.id)
        .join(Source, EvidenceSpan.source_id == Source.id)
        .where(ClaimEvidence.claim_id == claim.id)
    )
    rows = ce_result.all()

    supporting = []
    has_counter = False
    source_ids: set[str] = set()

    for ce, ev, src in rows:
        if ce.relation == "counter":
            has_counter = True
            continue
        source_ids.add(str(ev.source_id))
        supporting.append({
            "evidence_id": str(ev.id),
            "quote": ev.verbatim_quote[:300],
            "source_title": src.title or "",
            "source_reliability": src.domain_reliability,
            "source_id": str(src.id),
        })

    return {
        "supporting": supporting,
        "has_counter": has_counter,
        "source_ids": source_ids,
    }


async def verify_single_claim(claim: Claim, db: AsyncSession) -> VerificationLevel:
    """Verify a single claim using the hybrid approach. Returns new verification level.

    Reusable from both the pipeline node and post-ingestion upgrade.
    """
    evidence_data = await _load_claim_evidence(claim, db)

    # --- Rule-based pre-filter ---
    if claim.claim_type in ("opinion", "report"):
        if claim.attribution and claim.attribution.get("who"):
            return VerificationLevel.OBSERVED
        return VerificationLevel.UNVERIFIED

    if evidence_data["has_counter"]:
        return VerificationLevel.DISPUTED

    if not evidence_data["supporting"]:
        return VerificationLevel.UNVERIFIED

    # --- LLM verification for FACT claims with evidence ---
    result = await _llm_verify_batch(
        [{
            "claim_id": str(claim.id),
            "statement": claim.statement[:500],
            "evidence": evidence_data["supporting"],
            "independent_source_count": len(evidence_data["source_ids"]),
        }]
    )
    if result:
        return _parse_verification_level(result[0].get("verification_level", "UNVERIFIED"))

    # LLM failed or returned empty — fall back to deterministic rules
    reliabilities = [e["source_reliability"] for e in evidence_data["supporting"]]
    return _rule_based_fallback(evidence_data["source_ids"], reliabilities)


async def _llm_verify_batch(claims_payload: list[dict]) -> list[dict]:
    """Call LLM to verify a batch of claims. Returns parsed results."""
    prompt = json.dumps({"claims": claims_payload})
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
    """Parse LLM output to VerificationLevel enum."""
    mapping = {
        "VERIFIED": VerificationLevel.VERIFIED,
        "OBSERVED": VerificationLevel.OBSERVED,
        "UNVERIFIED": VerificationLevel.UNVERIFIED,
        "DISPUTED": VerificationLevel.DISPUTED,
    }
    return mapping.get(level_str.upper(), VerificationLevel.UNVERIFIED)


def _rule_based_fallback(source_ids: set[str], source_reliabilities: list[str]) -> VerificationLevel:
    """Fallback rule-based verification when LLM is unavailable."""
    if len(source_ids) >= 2:
        return VerificationLevel.VERIFIED
    if len(source_ids) == 1 and "high" in source_reliabilities:
        return VerificationLevel.VERIFIED
    if len(source_ids) == 1 and "medium" in source_reliabilities:
        return VerificationLevel.OBSERVED
    return VerificationLevel.UNVERIFIED


async def verify_claims(state: PipelineState) -> dict:
    """LangGraph node: verify claims using hybrid rule-based + LLM approach."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase4_claims", "verify", "Verifying claims"))
    update_generation_progress(run_id, "phase4_claims", "verifier", 42.0)

    claim_ids = state.get("claim_ids", [])
    if not claim_ids:
        return {"claim_ids": claim_ids}

    async with async_session_factory() as db:
        # Load all claims and their evidence
        claims_for_llm: list[tuple[Claim, dict]] = []
        prefiltered: dict[str, VerificationLevel] = {}

        for cid in claim_ids:
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            claim = result.scalar_one_or_none()
            if not claim:
                continue

            evidence_data = await _load_claim_evidence(claim, db)

            # --- Rule-based pre-filter (fast, no LLM) ---
            if claim.claim_type in ("opinion", "report"):
                if claim.attribution and claim.attribution.get("who"):
                    level = VerificationLevel.OBSERVED
                else:
                    level = VerificationLevel.UNVERIFIED
                prefiltered[cid] = level
                claim.verification_level = level
                publish_event(run_id, claim_verified(cid, level))
                continue

            if evidence_data["has_counter"]:
                prefiltered[cid] = VerificationLevel.DISPUTED
                claim.verification_level = VerificationLevel.DISPUTED
                publish_event(run_id, claim_verified(cid, VerificationLevel.DISPUTED))
                continue

            if not evidence_data["supporting"]:
                prefiltered[cid] = VerificationLevel.UNVERIFIED
                claim.verification_level = VerificationLevel.UNVERIFIED
                publish_event(run_id, claim_verified(cid, VerificationLevel.UNVERIFIED))
                continue

            # Needs LLM verification
            claims_for_llm.append((claim, evidence_data))

        # --- LLM verification in batches ---
        llm_needs_research = False
        for batch_start in range(0, len(claims_for_llm), VERIFY_BATCH_SIZE):
            batch = claims_for_llm[batch_start:batch_start + VERIFY_BATCH_SIZE]
            batch_payload = []
            for claim, ev_data in batch:
                batch_payload.append({
                    "claim_id": str(claim.id),
                    "statement": claim.statement[:500],
                    "evidence": ev_data["supporting"],
                    "independent_source_count": len(ev_data["source_ids"]),
                })

            llm_results = await _llm_verify_batch(batch_payload)

            # Map LLM results by claim_id
            result_map = {r["claim_id"]: r for r in llm_results if isinstance(r, dict) and "claim_id" in r}

            for claim, ev_data in batch:
                cid = str(claim.id)
                llm_result = result_map.get(cid)

                if llm_result:
                    level = _parse_verification_level(llm_result.get("verification_level", "UNVERIFIED"))
                    if llm_result.get("needs_research"):
                        llm_needs_research = True
                else:
                    # LLM didn't return result for this claim — fallback to rule-based
                    reliabilities = [e["source_reliability"] for e in ev_data["supporting"]]
                    level = _rule_based_fallback(ev_data["source_ids"], reliabilities)

                claim.verification_level = level
                publish_event(run_id, claim_verified(cid, level))

        await db.commit()

    publish_event(run_id, step_completed("phase4_claims", "verify"))
    publish_event(run_id, progress(45.0, f"Verified {len(claim_ids)} claims"))

    # Check if any claims need more research
    iteration_counts = dict(state.get("iteration_counts", {}))
    verify_loops = iteration_counts.get("verify_research_loops", 0)
    needs_research = False

    if verify_loops < settings.max_verifier_research_loops:
        if llm_needs_research:
            needs_research = True
        else:
            # Also check rule-based: UNVERIFIED facts
            async with async_session_factory() as db:
                for cid in claim_ids:
                    result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
                    claim = result.scalar_one_or_none()
                    if claim and claim.claim_type == "fact" and claim.verification_level == VerificationLevel.UNVERIFIED:
                        needs_research = True
                        break

    if needs_research:
        iteration_counts["verify_research_loops"] = verify_loops + 1

    iteration_counts["verify_needs_research"] = needs_research

    return {
        "claim_ids": claim_ids,
        "current_phase": "phase4_claims",
        "iteration_counts": iteration_counts,
    }
