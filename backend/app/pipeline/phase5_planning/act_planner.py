"""Phase 5.2: Act Planner — beat sheet per act."""

import json
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.claim import Claim
from app.models.evidence import EvidenceSpan
from app.models.claim import ClaimEvidence
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event
from app.realtime.events import act_planned, step_started, step_completed

logger = logging.getLogger(__name__)

ACT_PLANNER_SYSTEM = """You are a podcast act planner creating a beat sheet (scene list).
Given an act's claims, evidence, and quotas, create a detailed scene breakdown.

Return JSON:
{
  "beat_sheet": [
    {
      "scene": 1,
      "type": "hook|explanation|debate|case_study|transition|summary|takeaway",
      "description": "what happens in this scene",
      "claim_ids": ["id"],
      "evidence_ids": ["id"],
      "estimated_duration_seconds": 45,
      "transition_note": null,
      "notes": "Host A asks, Host B responds"
    }
  ]
}

Scene types:
- hook: provocative opening question/statement
- explanation: teaching a concept with examples
- debate: discussing conflicting views
- case_study: detailed real-world example
- transition: bridge between topics
- summary: recap key points
- takeaway: actionable insight"""


async def plan_act(state: PipelineState) -> dict:
    """LangGraph node: create beat sheets for all acts."""
    run_id = state["generation_run_id"]

    async with async_session_factory() as db:
        if not state.get("manifest_id"):
            return {"manifest_id": ""}

        result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
        )
        manifest = result.scalar_one_or_none()
        if not manifest or not manifest.acts_json:
            return {"manifest_id": state["manifest_id"]}

        acts = manifest.acts_json

        for act in acts:
            act_num = act.get("act_number", 0)
            publish_event(run_id, step_started("phase5_planning", f"plan_act_{act_num}", f"Planning Act {act_num}"))

            # Load claims for this act
            act_claims = []
            for cid in act.get("claim_ids", []):
                r = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
                c = r.scalar_one_or_none()
                if c:
                    act_claims.append({
                        "id": str(c.id), "type": c.claim_type,
                        "verification": c.verification_level, "statement": c.statement[:300],
                    })

            # Load evidence for act claims
            act_evidence = []
            for cid in act.get("claim_ids", []):
                ce_result = await db.execute(select(ClaimEvidence).where(ClaimEvidence.claim_id == uuid.UUID(cid)))
                for ce in ce_result.scalars():
                    ev_result = await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == ce.evidence_id))
                    ev = ev_result.scalar_one_or_none()
                    if ev:
                        act_evidence.append({
                            "id": str(ev.id), "asset_type": ev.asset_type,
                            "quote": ev.verbatim_quote[:200], "narrative_score": ev.narrative_value_score,
                        })

            prompt = json.dumps({
                "act_number": act_num,
                "act_title": act.get("title", ""),
                "estimated_duration_seconds": act.get("estimated_duration_seconds", 300),
                "content_quotas": act.get("content_quotas", {}),
                "claims": act_claims,
                "evidence": act_evidence[:20],
            })

            try:
                raw = await call_sonnet_structured(system=ACT_PLANNER_SYSTEM, prompt=prompt, temperature=0.5)
                beat_data = parse_llm_json(raw)
                act["beat_sheet"] = beat_data.get("beat_sheet", [])
            except Exception:
                logger.error("Act planning failed for act %d", act_num, exc_info=True)
                act["beat_sheet"] = []

            scene_count = len(act.get("beat_sheet", []))
            publish_event(run_id, act_planned(act_num, scene_count))
            publish_event(run_id, step_completed("phase5_planning", f"plan_act_{act_num}"))

        manifest.acts_json = acts
        await db.commit()

    return {"manifest_id": state["manifest_id"], "current_phase": "phase5_planning"}
