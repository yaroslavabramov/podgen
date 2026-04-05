"""Phase 5.1: Showrunner — high-level episode planning."""

import json
import logging
import math
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
from app.pipeline.short_ids import ShortIdMap, shorten_claim_dicts, restore_id_list
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, progress

logger = logging.getLogger(__name__)

SHOWRUNNER_SYSTEM = """You are a podcast showrunner planning the episode structure.
Given claims, conflicts, duration target, and show bible, create an act structure that feels like a strong episode arc, not a bucket of claims.

Return a JSON object shaped like:
{
  "act_count": <integer from 2 to 6>,
  "acts": [
    {
      "act_number": 1,
      "story_role": "setup|complicate|explain|resolve|payoff",
      "title": "Act title",
      "purpose": "one-sentence editorial function of this act",
      "internal_arc": "short phrase describing how this act moves internally",
      "opening_question": "listener-facing question or tension that opens this act",
      "ending_turn": "what changes, lands, or becomes clearer by the end of the act",
      "focus_branch_ids": ["b1", "b3"],
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
- Language:
  - All human-readable fields (`title`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`) MUST be in `output_language` from the user payload.
  - Do not mix languages inside one field.
- Plan an episode, not an outline dump. Every act must have a distinct job in the listener experience.
- Prefer a clean macro-arc: setup -> complication -> explanation -> consequence/payoff. Choose the act count that best fits the material and duration; do not default to 3 acts just because it is common.
- Start from the strongest episode-level question: why is this system strange, how does it work, and why does it matter?
- Each act should answer one part of that question and hand off naturally to the next act.
- For each act, define a short `internal_arc` that describes how the act moves, not just what topic it covers.
- Adjacent acts should usually have different `internal_arc` shapes. Do not make every act feel like question -> explanation -> takeaway.
- Curate aggressively: each act should keep only claims that make a distinct editorial contribution to its arc. If multiple claims describe the same mechanism, rule, or fact — even with different wording or from different sources — keep only the single strongest formulation and drop the rest entirely. Do not assign them to other acts either. Five claims that each say something different are worth more than ten where half restate the same point.
- Each claim appears in exactly ONE act (no duplication)
- The payload includes `total_claim_count`. Whatever `act_count` you choose, keep every act at or below `max(8, ceil(total_claim_count / act_count * 1.5))` claims.
- Distribute claim load roughly in proportion to each act's `estimated_duration_seconds`. Do not put a huge claim pile into one short act while neighboring acts stay thin.
- If one branch yields more strong claims than a single act can hold cleanly, keep the strongest claims in that act and leave the rest unused rather than overloading the episode.
- Do not spread the same proposition core across multiple acts just because it is relevant in several places. Pick the best act for it.
- If two claims from different branches are near-paraphrases or materially describe the same mechanism, treat them as one explanatory point. Keep the strongest or best-fitting one in the best act and avoid assigning the other as fresh explanatory backbone in a later act.
- Branch coherence is a hint, not a rule. Do not keep branch material together when that would make two acts explain the same thing with different claim IDs.
- Prefer one act to establish a mechanism and later acts to do something new with it: stress-test it, show its cost, compare it to another path, show the legal lock-in, or widen the stakes.
- One historical event or institutional decision should usually have one main dramatic home. If several claims describe the same decision from different dates or angles, let one act own the reveal and use the rest only as brief callbacks or consequences.
- Listener novelty matters more than branch coverage. A later act should not feel like the same explanation in a new folder.
- If one act receives several claims from different branches that all describe the same mechanism family, prefer a lean explanatory backbone over exhaustive inclusion. Keep only the strongest 2-3 as fresh explanatory load unless the extras clearly enable different scene moves.
- One act may own a mechanism family, but it should not become overcrowded with near-duplicate explanatory claims. Exhaustive branch inclusion is less important than keeping the listener's sense of forward motion.
- Avoid acts that all do the same thing. Do not make every act a generic explanation block.
- Prefer acts organized around one dominant branch or a tight pair of related branches, not a random mix of distant topics.
- Comparative material should stay selective. One strong external comparator is usually enough for one act beat; do not load an act with multiple unrelated outside mini-histories unless they clearly serve one editorial point.
- DISPUTED claims MUST have a "debate moment" (conflicts >= 1 in that act's quotas)
- Act count: 2-6 based on duration and style
- Verified facts should be front-loaded in acts where audience needs grounding
- Claims with direct support and stronger source diversity should form the explanatory backbone
- Claims with illustrative evidence are good candidates for hooks, case-study moments, and act transitions
- Use claims with illustrative evidence to create vivid openings, examples, and pivots instead of stacking abstract explanation.
- Last act should include takeaways and a clear "why this matters" payoff
- Vary `ending_turn` shapes across acts. Not every act should end with a synthesis or "the listener now understands" landing. Some acts can end with an open question that the next act picks up, a concrete image or case that lingers, a tension that sharpens rather than resolves, or a cost/tradeoff that reframes the stakes. The act planner will use `ending_turn` to choose the final scene type, so variety here drives variety downstream.
- Titles should sound like editorial sections of a strong episode, not generic chapter labels"""


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
                    "statement": claim.statement,
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

        # Build short-ID mapping to reduce LLM token waste and typos
        all_claim_ids = [str(cs["id"]) for cs in claims_summary]
        sid = ShortIdMap(all_claim_ids)
        short_claims = shorten_claim_dicts(claims_summary, sid)
        short_disputed = sid.shorten_list(disputed_ids)
        short_conflicts = [
            {**c, "claim_ids": sid.shorten_list(c.get("claim_ids", []))}
            for c in conflicts_summary
        ]

        prompt_payload = _build_showrunner_prompt_payload(
            duration_target_seconds=state["duration_target"],
            output_language=state.get("language", "en"),
            show_style=bible.show_style if bible else "Conversational",
            rigor_policy=bible.rigor_policy if bible else "",
            claims=short_claims,
            conflicts=short_conflicts,
            disputed_claim_ids=short_disputed,
            duration_fill_layers=manifest.duration_fill_layers if manifest else [],
        )
        prompt = json.dumps(prompt_payload, ensure_ascii=False)
        raw = ""
        parsed_plan = None
        try:
            raw = await call_sonnet_structured(system=SHOWRUNNER_SYSTEM, prompt=prompt, temperature=0.5)
            parsed_plan = parse_llm_json(raw)
            plan = _normalize_plan(parsed_plan, state["duration_target"], sid=sid)
            plan = _rebalance_act_claim_distribution(plan, claim_lookup={claim["id"]: claim for claim in claims_summary})
            plan = _log_act_claim_counts(plan)
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
            act_conflicts = [
                conflict
                for conflict in conflicts_summary
                if set(conflict.get("claim_ids", [])) & set(act.get("claim_ids", []))
            ]
            if (act_disputed or act_conflicts) and act.get("content_quotas", {}).get("conflicts", 0) < 1:
                act.setdefault("content_quotas", {})["conflicts"] = max(1, len(act_disputed) or len(act_conflicts))

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


def _build_showrunner_prompt_payload(
    *,
    duration_target_seconds: int,
    output_language: str,
    show_style: str,
    rigor_policy: str,
    claims: list[dict],
    conflicts: list[dict],
    disputed_claim_ids: list[str],
    duration_fill_layers: list | None,
) -> dict:
    return {
        "duration_target_seconds": duration_target_seconds,
        "output_language": str(output_language or "en").strip() or "en",
        "show_style": show_style,
        "rigor_policy": rigor_policy,
        "total_claim_count": len(claims),
        "claims": claims,
        "conflicts": conflicts,
        "disputed_claim_ids": disputed_claim_ids,
        "duration_fill_layers": duration_fill_layers or [],
    }


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


def _normalize_plan(plan, duration_target: int, *, sid: ShortIdMap | None = None) -> dict:
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

        story_role = act.get("story_role")
        if not isinstance(story_role, str) or not story_role.strip():
            story_role = "explain"

        purpose = act.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            purpose = title.strip()

        internal_arc = act.get("internal_arc")
        if not isinstance(internal_arc, str):
            internal_arc = ""

        opening_question = act.get("opening_question")
        if not isinstance(opening_question, str):
            opening_question = ""

        ending_turn = act.get("ending_turn")
        if not isinstance(ending_turn, str):
            ending_turn = ""

        normalized_acts.append(
            {
                "act_number": act_number,
                "story_role": story_role.strip(),
                "title": title.strip(),
                "purpose": purpose.strip(),
                "internal_arc": internal_arc.strip(),
                "opening_question": opening_question.strip(),
                "ending_turn": ending_turn.strip(),
                "focus_branch_ids": _string_list(act.get("focus_branch_ids")),
                "estimated_duration_seconds": _positive_int(
                    act.get("estimated_duration_seconds"),
                    default_act_duration,
                ),
                "claim_ids": restore_id_list(act.get("claim_ids"), sid) if sid else _uuid_list(act.get("claim_ids")),
                "conflict_brief_ids": _uuid_list(act.get("conflict_brief_ids")),
                "content_quotas": content_quotas,
            }
        )

    if not normalized_acts:
        raise RuntimeError("Showrunner produced no valid acts")

    return {"act_count": len(normalized_acts), "acts": normalized_acts}


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _log_act_claim_counts(plan: dict) -> dict:
    """Log claim counts per act for diagnostics (no filtering)."""
    for act in plan.get("acts", []):
        act_num = act.get("act_number", "?")
        n_claims = len(act.get("claim_ids", []) or [])
        dur = act.get("estimated_duration_seconds", 0)
        logger.info("Showrunner act %s: %d claims, %ds", act_num, n_claims, dur)
    return plan


def _compute_max_claims_per_act(total_claim_count: int, act_count: int) -> int:
    if total_claim_count <= 0 or act_count <= 0:
        return 0
    return min(total_claim_count, max(8, math.ceil((total_claim_count / act_count) * 1.5)))


def _duration_weighted_claim_targets(acts: list[dict[str, object]], assigned_claim_count: int) -> dict[int, float]:
    if assigned_claim_count <= 0:
        return {}
    durations = {
        _positive_int(act.get("act_number"), idx + 1): max(1, _positive_int(act.get("estimated_duration_seconds"), 0))
        for idx, act in enumerate(acts)
    }
    total_duration = sum(durations.values()) or len(durations)
    return {
        act_number: assigned_claim_count * (duration / total_duration)
        for act_number, duration in durations.items()
    }


def _pick_claim_rebalance_target(
    *,
    source_index: int,
    claim_id: str,
    acts: list[dict[str, object]],
    claim_lookup: dict[str, dict],
    max_claims_per_act: int,
    target_loads: dict[int, float],
) -> dict[str, object] | None:
    claim_branches = {
        branch_id
        for branch_id in (claim_lookup.get(claim_id, {}).get("branch_ids") or [])
        if isinstance(branch_id, str) and branch_id.strip()
    }
    candidates: list[tuple[tuple[object, ...], dict[str, object]]] = []

    for idx, act in enumerate(acts):
        if idx == source_index:
            continue
        if abs(idx - source_index) != 1:
            continue
        current_claim_ids = _uuid_list(act.get("claim_ids"))
        if len(current_claim_ids) >= max_claims_per_act:
            continue

        focus_branch_ids = set(_string_list(act.get("focus_branch_ids")))
        branch_overlap = 1 if claim_branches and focus_branch_ids & claim_branches else 0
        act_number = _positive_int(act.get("act_number"), idx + 1)
        preferred_gap = target_loads.get(act_number, 0.0) - len(current_claim_ids)
        headroom = max_claims_per_act - len(current_claim_ids)
        sort_key = (
            -branch_overlap,
            -max(preferred_gap, 0.0),
            -headroom,
            act_number,
        )
        candidates.append((sort_key, act))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _rebalance_act_claim_distribution(
    plan: dict,
    *,
    claim_lookup: dict[str, dict],
) -> dict:
    acts_raw = plan.get("acts")
    if not isinstance(acts_raw, list) or not acts_raw:
        return plan

    acts: list[dict[str, object]] = [act for act in acts_raw if isinstance(act, dict)]
    if not acts:
        return plan

    seen_claim_ids: set[str] = set()
    for idx, act in enumerate(acts):
        deduped_claim_ids: list[str] = []
        for claim_id in _uuid_list(act.get("claim_ids")):
            if claim_id in seen_claim_ids:
                logger.warning(
                    "Showrunner duplicated claim %s across acts; keeping first occurrence and dropping from act %s",
                    claim_id,
                    _positive_int(act.get("act_number"), idx + 1),
                )
                continue
            seen_claim_ids.add(claim_id)
            deduped_claim_ids.append(claim_id)
        act["claim_ids"] = deduped_claim_ids

    assigned_claim_count = sum(len(_uuid_list(act.get("claim_ids"))) for act in acts)
    max_claims_per_act = _compute_max_claims_per_act(assigned_claim_count, len(acts))
    if max_claims_per_act <= 0:
        return plan
    target_loads = _duration_weighted_claim_targets(acts, assigned_claim_count)
    overflow_queue: list[tuple[int, str]] = []

    for idx, act in enumerate(acts):
        claim_ids = _uuid_list(act.get("claim_ids"))
        if len(claim_ids) <= max_claims_per_act:
            act["claim_ids"] = claim_ids
            continue
        overflow = claim_ids[max_claims_per_act:]
        act["claim_ids"] = claim_ids[:max_claims_per_act]
        act_number = _positive_int(act.get("act_number"), idx + 1)
        overflow_queue.extend((idx, claim_id) for claim_id in overflow)
        logger.warning(
            "Showrunner overloaded act %s with %d claims (ceiling %d); trimming %d overflow claim(s)",
            act_number,
            len(claim_ids),
            max_claims_per_act,
            len(overflow),
        )

    for source_index, claim_id in overflow_queue:
        source_act_number = _positive_int(acts[source_index].get("act_number"), source_index + 1)
        target_act = _pick_claim_rebalance_target(
            source_index=source_index,
            claim_id=claim_id,
            acts=acts,
            claim_lookup=claim_lookup,
            max_claims_per_act=max_claims_per_act,
            target_loads=target_loads,
        )
        if target_act is None:
            logger.warning(
                "Showrunner dropped overflow claim %s from act %s after balancing because no nearby act had capacity",
                claim_id,
                source_act_number,
            )
            continue

        target_claim_ids = _uuid_list(target_act.get("claim_ids"))
        target_claim_ids.append(claim_id)
        target_act["claim_ids"] = target_claim_ids
        logger.warning(
            "Showrunner rebalanced overflow claim %s from act %s to act %s",
            claim_id,
            source_act_number,
            _positive_int(target_act.get("act_number"), 0),
        )

    plan["acts"] = acts
    return plan


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
