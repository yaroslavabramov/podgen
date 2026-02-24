"""Phase 5.1: Showrunner — high-level episode planning."""

import json
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.show_bible import ShowBible
from app.models.claim import Claim, VerificationLevel
from app.models.conflict_brief import ConflictBrief
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.state import PipelineState
from app.pipeline.llm_json import parse_llm_json
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, progress

logger = logging.getLogger(__name__)

SHOWRUNNER_SYSTEM = """You are a podcast showrunner planning the episode structure.
Given claims, conflicts, duration target, and show bible, create an act structure.

Return JSON:
{
  "act_count": 3,
  "acts": [
    {
      "act_number": 1,
      "title": "Act title",
      "estimated_duration_seconds": 600,
      "claim_ids": ["id1", "id2"],
      "conflict_brief_ids": [],
      "content_quotas": {
        "verified_facts": 3,
        "attributed_opinions": 1,
        "narrative_assets": 1,
        "conflicts": 0,
        "takeaways": 1
      }
    }
  ]
}

Rules:
- Each claim appears in exactly ONE act (no duplication)
- DISPUTED claims MUST have a "debate moment" (conflicts >= 1 in that act's quotas)
- Act count: 2-6 based on duration and style
- Verified facts should be front-loaded in acts where audience needs grounding
- Last act should include takeaways"""


async def plan_episode(state: PipelineState) -> dict:
    """LangGraph node: create high-level act structure."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase5_planning", "Planning episode structure"))
    update_generation_progress(run_id, "phase5_planning", "showrunner", 50.0)

    async with async_session_factory() as db:
        # Load ShowBible
        bible = None
        if state.get("show_bible_id"):
            result = await db.execute(select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"])))
            bible = result.scalar_one_or_none()

        # Load claims
        claims_summary = []
        disputed_ids = []
        for cid in state.get("claim_ids", []):
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            claim = result.scalar_one_or_none()
            if claim:
                claims_summary.append({
                    "id": str(claim.id),
                    "type": claim.claim_type,
                    "verification": claim.verification_level,
                    "statement": claim.statement[:200],
                })
                if claim.verification_level == VerificationLevel.DISPUTED:
                    disputed_ids.append(str(claim.id))

        # Load conflict briefs
        conflicts_summary = []
        for cbid in state.get("conflict_brief_ids", []):
            result = await db.execute(select(ConflictBrief).where(ConflictBrief.id == uuid.UUID(cbid)))
            cb = result.scalar_one_or_none()
            if cb:
                conflicts_summary.append({
                    "id": str(cb.id),
                    "what": cb.what_conflicts[:200],
                    "claim_ids": [str(c) for c in cb.claim_ids],
                })

        # Load manifest for duration layers
        manifest = None
        if state.get("manifest_id"):
            result = await db.execute(select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"])))
            manifest = result.scalar_one_or_none()

        publish_event(run_id, step_started("phase5_planning", "structure", "Creating act structure"))

        prompt = json.dumps({
            "duration_target_seconds": state["duration_target"],
            "show_style": bible.show_style if bible else "Conversational",
            "rigor_policy": bible.rigor_policy if bible else "",
            "claims": claims_summary,
            "conflicts": conflicts_summary,
            "disputed_claim_ids": disputed_ids,
            "duration_fill_layers": manifest.duration_fill_layers if manifest else [],
        })

        raw = await call_sonnet_structured(system=SHOWRUNNER_SYSTEM, prompt=prompt, temperature=0.5)
        plan = _normalize_plan(parse_llm_json(raw), state["duration_target"])

        # Validate: all disputed claims have debate slots
        for act in plan.get("acts", []):
            act_disputed = [c for c in act.get("claim_ids", []) if c in disputed_ids]
            if act_disputed and act.get("content_quotas", {}).get("conflicts", 0) < 1:
                act.setdefault("content_quotas", {})["conflicts"] = len(act_disputed)

        # Update manifest
        if manifest:
            manifest.act_count = plan.get("act_count", len(plan.get("acts", [])))
            manifest.acts_json = plan.get("acts", [])
            await db.commit()

    publish_event(run_id, step_completed("phase5_planning", "structure"))
    publish_event(run_id, progress(55.0, f"Planned {plan.get('act_count', 0)} acts"))

    return {"manifest_id": state.get("manifest_id", ""), "current_phase": "phase5_planning"}


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _uuid_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            uuid.UUID(item)
        except ValueError:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _normalize_plan(plan, duration_target: int) -> dict:
    if isinstance(plan, dict):
        acts_raw = plan.get("acts")
    elif isinstance(plan, list):
        acts_raw = plan
    else:
        acts_raw = None

    if not isinstance(acts_raw, list):
        raise RuntimeError("Showrunner response does not contain an acts list")

    default_act_duration = max(180, duration_target // max(1, len(acts_raw)))
    normalized_acts: list[dict] = []

    for i, act in enumerate(acts_raw, start=1):
        if not isinstance(act, dict):
            continue
        act_number = _positive_int(act.get("act_number"), i)
        title = act.get("title")
        if not isinstance(title, str) or not title.strip():
            title = f"Act {act_number}"

        content_quotas = act.get("content_quotas")
        if not isinstance(content_quotas, dict):
            content_quotas = {}

        normalized_acts.append(
            {
                "act_number": act_number,
                "title": title.strip(),
                "estimated_duration_seconds": _positive_int(
                    act.get("estimated_duration_seconds"),
                    default_act_duration,
                ),
                "claim_ids": _uuid_list(act.get("claim_ids")),
                "conflict_brief_ids": _uuid_list(act.get("conflict_brief_ids")),
                "content_quotas": content_quotas,
            }
        )

    if not normalized_acts:
        raise RuntimeError("Showrunner produced no valid acts")

    return {"act_count": len(normalized_acts), "acts": normalized_acts}
