"""Phase 4.2: Verifier — triangulate and verify claims."""

import json
import logging
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models.claim import Claim, ClaimEvidence, VerificationLevel
from app.models.evidence import EvidenceSpan
from app.models.source import Source
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import step_started, step_completed, claim_verified, progress

logger = logging.getLogger(__name__)


async def verify_claims(state: PipelineState) -> dict:
    """LangGraph node: verify claims using triangulation algorithm."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase4_claims", "verify", "Verifying claims"))
    update_generation_progress(run_id, "phase4_claims", "verifier", 42.0)

    claim_ids = state.get("claim_ids", [])
    if not claim_ids:
        return {"claim_ids": claim_ids}

    async with async_session_factory() as db:
        for cid in claim_ids:
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            claim = result.scalar_one_or_none()
            if not claim:
                continue

            if claim.claim_type in ("opinion", "report"):
                # For OPINION/REPORT: check attribution exists
                if claim.attribution and claim.attribution.get("who"):
                    claim.verification_level = VerificationLevel.OBSERVED
                else:
                    claim.verification_level = VerificationLevel.UNVERIFIED
                publish_event(run_id, claim_verified(cid, claim.verification_level))
                continue

            # For FACT claims: triangulation
            ce_result = await db.execute(
                select(ClaimEvidence).where(
                    ClaimEvidence.claim_id == claim.id,
                    ClaimEvidence.relation == "supporting",
                )
            )
            supporting = ce_result.scalars().all()

            # Count independent sources
            source_ids = set()
            source_reliabilities = []
            has_counter = False

            for ce in supporting:
                ev_result = await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == ce.evidence_id))
                ev = ev_result.scalar_one_or_none()
                if ev:
                    source_ids.add(str(ev.source_id))
                    src_result = await db.execute(select(Source).where(Source.id == ev.source_id))
                    src = src_result.scalar_one_or_none()
                    if src:
                        source_reliabilities.append(src.domain_reliability)

            # Check for counter evidence
            counter_result = await db.execute(
                select(ClaimEvidence).where(
                    ClaimEvidence.claim_id == claim.id,
                    ClaimEvidence.relation == "counter",
                )
            )
            if counter_result.scalars().first():
                has_counter = True

            # Apply verification algorithm
            if has_counter:
                claim.verification_level = VerificationLevel.DISPUTED
            elif len(source_ids) >= 2:
                claim.verification_level = VerificationLevel.VERIFIED
            elif len(source_ids) == 1 and "high" in source_reliabilities:
                claim.verification_level = VerificationLevel.VERIFIED
            elif len(source_ids) == 1 and "medium" in source_reliabilities:
                claim.verification_level = VerificationLevel.OBSERVED
            else:
                claim.verification_level = VerificationLevel.UNVERIFIED

            publish_event(run_id, claim_verified(cid, claim.verification_level))

        await db.commit()

    publish_event(run_id, step_completed("phase4_claims", "verify"))
    publish_event(run_id, progress(45.0, f"Verified {len(claim_ids)} claims"))

    return {"claim_ids": claim_ids, "current_phase": "phase4_claims"}
