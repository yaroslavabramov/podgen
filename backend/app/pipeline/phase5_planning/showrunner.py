"""Phase 5.1: Showrunner — high-level episode planning."""

import json
import logging
import uuid

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.show_bible import ShowBible
from app.models.claim import Claim, VerificationLevel
from app.models.conflict_brief import ConflictBrief
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.phase5_planning.planning_context import load_planning_context
from app.pipeline.research_context import build_question_context_map
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
- Claims with direct support and stronger source diversity should form the explanatory backbone
- Claims with illustrative evidence are good candidates for hooks, case-study moments, and act transitions
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

        question_context_map = build_question_context_map(
            manifest.research_tree_json if manifest else None
        )
        claim_contexts, _ = await load_planning_context(
            db=db,
            claim_ids=state.get("claim_ids", []),
            generation_run_id=state["generation_run_id"],
            question_context_map=question_context_map,
        )
        claims_summary = []
        disputed_ids = []
        for cid in state.get("claim_ids", []):
            claim_summary = claim_contexts.get(cid)
            if not claim_summary:
                result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
                claim = result.scalar_one_or_none()
                if claim is None:
                    continue
                claim_summary = {
                    "id": str(claim.id),
                    "type": claim.claim_type,
                    "verification": claim.verification_level,
                    "statement": claim.statement[:200],
                    "supporting_evidence_count": 0,
                    "illustrative_evidence_count": 0,
                    "has_illustrative": False,
                    "branch_ids": [],
                    "question_ids": [],
                    "intents": [],
                    "direct_support_count": 0,
                    "contextual_support_count": 0,
                    "source_diversity": 0,
                    "quality_hints": {
                        "has_primary_support": False,
                        "has_high_reliability_support": False,
                        "temporary_only_support": False,
                    },
                }
            claims_summary.append(claim_summary)
            if claim_summary["verification"] == VerificationLevel.DISPUTED:
                disputed_ids.append(str(claim_summary["id"]))

        publish_event(run_id, step_started("phase5_planning", "structure", "Creating act structure"))

        prompt_payload = {
            "duration_target_seconds": state["duration_target"],
            "show_style": bible.show_style if bible else "Conversational",
            "rigor_policy": bible.rigor_policy if bible else "",
            "claims": claims_summary,
            "conflicts": conflicts_summary,
            "disputed_claim_ids": disputed_ids,
            "duration_fill_layers": manifest.duration_fill_layers if manifest else [],
        }
        prompt = json.dumps(prompt_payload, ensure_ascii=False)
        raw = ""
        parsed_plan = None
        try:
            raw = await call_sonnet_structured(system=SHOWRUNNER_SYSTEM, prompt=prompt, temperature=0.5)
            parsed_plan = parse_llm_json(raw)
            plan = _normalize_plan(parsed_plan, state["duration_target"])
        except Exception:
            _dump_showrunner_debug(
                run_id=run_id,
                status="llm_or_normalize_failed",
                input_payload=prompt_payload,
                llm_raw_output=raw,
                parsed_plan=parsed_plan,
                normalized_plan=None,
            )
            raise

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

    _dump_showrunner_debug(
        run_id=run_id,
        status="ok",
        input_payload=prompt_payload,
        llm_raw_output=raw,
        parsed_plan=parsed_plan,
        normalized_plan=plan,
    )

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


def _dump_showrunner_debug(
    *,
    run_id: str,
    status: str,
    input_payload: dict,
    llm_raw_output: str,
    parsed_plan: object,
    normalized_plan: dict | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="planning_showrunner",
        filename_stem=(
            f"run-{safe_filename_token(run_id)}_"
            f"{safe_filename_token(status)}"
        ),
        payload={
            "run_id": run_id,
            "status": status,
            "input": input_payload,
            "llm_output": {
                "raw_chars": len(llm_raw_output or ""),
                "raw": llm_raw_output,
                "parsed_type": type(parsed_plan).__name__ if parsed_plan is not None else None,
                "parsed": parsed_plan,
            },
            "normalized_plan": normalized_plan,
        },
    )
