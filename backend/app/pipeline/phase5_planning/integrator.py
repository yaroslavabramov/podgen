"""Phase 5.3: Integrator — check transitions, anti-duplication, complexity flow."""

import json
import logging
import time
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event
from app.realtime.events import step_started, step_completed, phase_completed, progress

logger = logging.getLogger(__name__)

INTEGRATOR_SYSTEM = """You are a podcast episode integrator checking the flow between acts.
Given all beat sheets, check:
1. Smooth transitions between acts
2. No duplicated claims across acts
3. Complexity builds gradually
4. Pacing alternates between dense facts and lighter stories

Return JSON:
{
  "transitions": [
    {"from_act": 1, "to_act": 2, "transition_note": "Bridge from history to mechanics"}
  ],
  "issues_found": [
    {"type": "duplicate_claim|pacing|complexity", "description": "...", "suggestion": "..."}
  ]
}"""


async def integrate_acts(state: PipelineState) -> dict:
    """LangGraph node: integrate and polish act transitions."""
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, step_started("phase5_planning", "integrate", "Integrating acts"))

    async with async_session_factory() as db:
        result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
        )
        manifest = result.scalar_one_or_none()
        if not manifest or not manifest.acts_json:
            return {"manifest_id": state["manifest_id"]}

        acts = manifest.acts_json
        prompt = json.dumps({"acts": acts})

        try:
            raw = await call_sonnet_structured(system=INTEGRATOR_SYSTEM, prompt=prompt, max_tokens=2048, temperature=0.4)
            integration = json.loads(raw)

            # Apply transition notes
            for tr in integration.get("transitions", []):
                from_idx = tr["from_act"] - 1
                if 0 <= from_idx < len(acts):
                    beat_sheet = acts[from_idx].get("beat_sheet", [])
                    if beat_sheet:
                        beat_sheet[-1]["transition_note"] = tr.get("transition_note", "")

            manifest.acts_json = acts
            await db.commit()
        except Exception:
            logger.warning("Integration step failed", exc_info=True)

    publish_event(run_id, step_completed("phase5_planning", "integrate"))
    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase5_planning", duration_ms))
    publish_event(run_id, progress(60.0, "Episode structure finalized"))

    return {"manifest_id": state["manifest_id"], "current_phase": "phase5_planning"}
