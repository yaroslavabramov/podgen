"""Phase 4.3: Conflict Resolver — create ConflictBriefs for DISPUTED claims."""

import json
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.claim import Claim, VerificationLevel
from app.models.conflict_brief import ConflictBrief
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event
from app.realtime.events import step_started, step_completed, conflict_detected, phase_completed, progress

import time

logger = logging.getLogger(__name__)

RESOLVE_SYSTEM = """You are a conflict analyst for a podcast pipeline.
Given disputed claims, create a conflict brief explaining the disagreement.

Return JSON:
{
  "what_conflicts": "description of the contradiction",
  "why_reason": "why sources disagree (methodology, timeframe, bias, etc.)",
  "how_to_present": "suggestion for how hosts should discuss this honestly"
}"""


async def resolve_conflicts(state: PipelineState) -> dict:
    """LangGraph node: create ConflictBriefs for DISPUTED claims."""
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, step_started("phase4_claims", "resolve_conflicts", "Analyzing contradictions"))

    claim_ids = state.get("claim_ids", [])
    conflict_brief_ids = list(state.get("conflict_brief_ids", []))

    async with async_session_factory() as db:
        # Find DISPUTED claims
        disputed = []
        for cid in claim_ids:
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            claim = result.scalar_one_or_none()
            if claim and claim.verification_level == VerificationLevel.DISPUTED:
                disputed.append(claim)

        if not disputed:
            publish_event(run_id, step_completed("phase4_claims", "resolve_conflicts"))
            duration_ms = int((time.time() - t0) * 1000)
            publish_event(run_id, phase_completed("phase4_claims", duration_ms))
            return {"conflict_brief_ids": conflict_brief_ids, "current_phase": "phase4_claims"}

        for claim in disputed:
            prompt = json.dumps({
                "disputed_claim": claim.statement,
                "claim_type": claim.claim_type,
                "notes": claim.notes or "",
            }, ensure_ascii=False)

            try:
                raw = await call_sonnet_structured(
                    system=RESOLVE_SYSTEM,
                    prompt=prompt,
                    max_tokens=1024,
                    temperature=0.4,
                )
                data = parse_llm_json(raw)

                brief = ConflictBrief(
                    id=uuid.uuid4(),
                    project_id=uuid.UUID(state["project_id"]),
                    claim_ids=[claim.id],
                    what_conflicts=data["what_conflicts"],
                    why_reason=data["why_reason"],
                    how_to_present=data["how_to_present"],
                )
                db.add(brief)
                await db.flush()
                conflict_brief_ids.append(str(brief.id))

                publish_event(run_id, conflict_detected(data["what_conflicts"][:200]))
            except Exception:
                logger.warning("Conflict resolution failed for claim %s", claim.id, exc_info=True)

        await db.commit()

    publish_event(run_id, step_completed("phase4_claims", "resolve_conflicts"))
    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase4_claims", duration_ms))
    publish_event(run_id, progress(48.0, f"Resolved {len(conflict_brief_ids)} conflicts"))

    return {"conflict_brief_ids": conflict_brief_ids, "current_phase": "phase4_claims"}
