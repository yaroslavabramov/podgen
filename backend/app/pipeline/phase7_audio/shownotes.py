"""Phase 7.2: Shownotes Generation."""

import json
import logging
import time
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode import Episode
from app.models.script_segment import ScriptSegment
from app.models.claim import Claim, ClaimEvidence
from app.models.evidence import EvidenceSpan
from app.models.source import Source
from app.pipeline.llm_json import parse_llm_json
from app.models.conflict_brief import ConflictBrief
from app.pipeline.state import PipelineState
from app.services.llm import call_haiku_structured
from app.realtime.publisher import publish_event
from app.realtime.events import step_started, step_completed, phase_completed, progress

logger = logging.getLogger(__name__)

SHOWNOTES_SYSTEM = """You are a podcast shownotes generator.
Given claims, evidence, and sources, create structured shownotes.

Return JSON:
{
  "sections": [
    {
      "title": "Section title",
      "references": [
        {
          "claim": "claim statement",
          "verification": "VERIFIED|OBSERVED|DISPUTED",
          "sources": [
            {"title": "source title", "url": "url", "date": "date", "quote": "key quote"}
          ]
        }
      ],
      "conflicts": [
        {
          "topic": "topic",
          "positions": ["position A", "position B"],
          "reason": "why they disagree"
        }
      ]
    }
  ]
}"""


async def build_shownotes(state: PipelineState) -> dict:
    """LangGraph node: generate structured shownotes."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase7_audio", "shownotes", "Building shownotes"))

    async with async_session_factory() as db:
        # Gather claims and their sources
        claims_with_sources = []
        for cid in state.get("claim_ids", []):
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            claim = result.scalar_one_or_none()
            if not claim:
                continue

            # Get evidence and sources
            ce_result = await db.execute(select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id))
            sources_info = []
            for ce in ce_result.scalars():
                ev_result = await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == ce.evidence_id))
                ev = ev_result.scalar_one_or_none()
                if ev:
                    src_result = await db.execute(select(Source).where(Source.id == ev.source_id))
                    src = src_result.scalar_one_or_none()
                    if src:
                        sources_info.append({
                            "title": src.title or "Untitled",
                            "url": src.original_url or "",
                            "quote": ev.verbatim_quote[:200],
                        })

            claims_with_sources.append({
                "claim": claim.statement[:300],
                "type": claim.claim_type,
                "verification": claim.verification_level,
                "sources": sources_info[:3],
            })

        # Gather conflicts
        conflicts_data = []
        for cbid in state.get("conflict_brief_ids", []):
            result = await db.execute(select(ConflictBrief).where(ConflictBrief.id == uuid.UUID(cbid)))
            cb = result.scalar_one_or_none()
            if cb:
                conflicts_data.append({
                    "what": cb.what_conflicts,
                    "why": cb.why_reason,
                    "how": cb.how_to_present,
                })

        prompt = json.dumps({
            "claims": claims_with_sources,
            "conflicts": conflicts_data,
            "language": state["language"],
        })

        try:
            raw = await call_haiku_structured(system=SHOWNOTES_SYSTEM, prompt=prompt, max_tokens=4096, temperature=0.3)
            shownotes = parse_llm_json(raw)
        except Exception:
            logger.error("Shownotes generation failed", exc_info=True)
            shownotes = {"sections": []}

        # Update Episode
        if state.get("episode_id"):
            result = await db.execute(select(Episode).where(Episode.id == uuid.UUID(state["episode_id"])))
            episode = result.scalar_one_or_none()
            if episode:
                episode.shownotes_json = shownotes
                episode.quality_metrics = state.get("quality_signals", {})
                await db.commit()

    publish_event(run_id, step_completed("phase7_audio", "shownotes"))
    t_ms = int(time.time() * 1000)
    publish_event(run_id, phase_completed("phase7_audio", t_ms))
    publish_event(run_id, progress(100.0, "Episode complete!"))

    return {"episode_id": state.get("episode_id", ""), "current_phase": "phase7_audio"}
