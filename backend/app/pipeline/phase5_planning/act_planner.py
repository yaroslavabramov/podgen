"""Phase 5.2: Act Planner — beat sheet per act."""

import asyncio
import copy
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.phase5_planning.planning_context import load_planning_context
from app.pipeline.research_context import build_question_context_map
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured, call_haiku_structured
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.short_ids import ShortIdMap, restore_id_list, shorten_claim_dicts
from app.pipeline.step_control import StepExecutionStopped
from app.realtime.publisher import publish_event
from app.realtime.events import act_planned, step_started, step_completed

logger = logging.getLogger(__name__)
ACT_PLANNER_REQUEST_RETRIES = 2
ACT_PLANNER_QUALITY_RETRIES = 2
ACT_PLANNER_MAX_TOTAL_ATTEMPTS = ACT_PLANNER_REQUEST_RETRIES + ACT_PLANNER_QUALITY_RETRIES
MAX_SUPPORT_EVIDENCE_PER_ACT = 30
MAX_DIRECT_SUPPORT_PER_CLAIM = 1
MAX_ILLUSTRATIVE_SUPPORT_PER_CLAIM = 1
MAX_EXISTING_EVIDENCE_IDS_PER_SCENE = 2
MAX_PRIOR_PRIMARY_CLAIMS = 12
MAX_RECENT_ACT_STRUCTURES = 2
PRIOR_PRIMARY_STATEMENT_CHARS = 200
MAX_CALLBACK_ENDING_CALLBACK_CLAIMS = 2
MAX_CALLBACK_ENDING_DESCRIPTION_CHARS = 320
MAX_BEAT_CARD_DESCRIPTION_CHARS = 250
SCENE_JOB_VALUES = {
    "reframe",
    "cost",
    "contrast",
    "bridge",
    "synthesize",
    "apply",
    "stress_test",
}
ENDING_SCENE_JOB_VALUES = {"reframe", "cost", "contrast", "bridge", "synthesize"}
MAX_SCENE_CLARITY_TEXT_CHARS = 180
MAX_SCENE_CLARITY_ITEMS = 4
MAX_SCENE_CLARITY_ITEM_CHARS = 90

ACT_PLANNER_SYSTEM = """You are a podcast act planner creating a beat sheet (scene list).
Given an act contract and claims with support bundles, build the smallest sequence of scenes that gives the listener clear internal movement through the act. Scenes are jobs, not slots.

Return JSON:
{
  "scene_strategy": "short phrase describing the act's internal movement",
  "beat_sheet": [
    {
      "scene": 1,
      "type": "hook|explanation|debate|case_study|transition|summary|takeaway",
      "scene_job": "reframe|cost|contrast|bridge|synthesize|apply|stress_test|null",
      "description": "1-3 sentence beat card (max 250 characters) describing what changes in this scene",
      "primary_claim_ids": ["id"],
      "callback_claim_ids": ["id"],
      "listener_state_before": "what the listener is assumed to understand before this scene",
      "listener_state_after": "what should become clear by the end of this scene",
      "must_make_clear": ["short listener-facing clarity goals"],
      "must_not_assume": ["background the scene is not allowed to assume"],
      "new_terms_to_introduce": ["technical or institutional terms that need gentle introduction"],
      "required_bridge_from_previous_scene": "short note about what unresolved point must carry over, or null",
      "evidence_ids": ["id"],
      "estimated_duration_seconds": 45,
      "transition_note": "short bridge to the next scene or null",
      "notes": "short host-dynamic or production cue, not script prose"
    }
  ]
}

Scene types are tools, not mandatory slots. Any type except `transition` can open or close an act if it performs the needed function:
- hook: provocative opening question/statement; can close an act by leaving a deliberate open question
- explanation: teaching a concept with examples; can close an act when the mechanism itself is the payoff
- debate: discussing conflicting views; can close an act with unresolved tension or a sharp tradeoff
- case_study: detailed real-world example; can close an act with a vivid concrete image that lingers
- transition: bridge between topics
- summary: recap key points
- takeaway: uses its own claims to recontextualize the act — not summarize it

Use claim/evidence metadata:
- `has_illustrative` and `illustrative_evidence_count` indicate narrative hooks
- `direct_support_count` and `source_diversity` indicate factual backbone
- each claim includes `support_bundle.direct` and optional `support_bundle.illustrative`
- bundled illustrative evidence is good for hooks, case-study beats, and vivid transitions

Valid act-shape examples:
- `case_study -> explanation -> debate`: opens with a concrete event, extracts the mechanism, then lands the tradeoff
- `explanation -> case_study -> takeaway`: starts with the rule, shows it operating in practice, then widens the implication
- `hook -> explanation -> case_study -> open ending`: opens a question, answers the core model, shows the case, then closes on a sharper unresolved question
- These are examples, not templates. Do not default to one fixed outer shape across every act.

Rules:
- Language:
  - All human-readable output fields (`scene_strategy`, `description`, `transition_note`, `notes`, `listener_state_before`, `listener_state_after`, `required_bridge_from_previous_scene`, list items in `must_make_clear`, `must_not_assume`, `new_terms_to_introduce`) MUST be in `output_language` from the user payload.
  - Keep the output in the same language as the act contract unless `output_language` says otherwise.
  - Do not mix languages inside one field.
- Mission:
  - The act must feel like it goes somewhere. A good beat sheet is a sequence of distinct listener moves, not a bucket of facts.
  - Respect the act contract:
    - `story_role` says what kind of movement this act should deliver
    - `purpose` says why the act exists
    - `internal_arc` says how the act should move internally
    - the act should engage `opening_question` early, but this does not require a dedicated `hook` scene
    - the act must deliver `ending_turn`, but this does not require a dedicated `takeaway` or `summary` scene
- Planning procedure:
  - Before drafting scenes, choose a short `scene_strategy` that captures how this act moves internally.
  - Decide the minimum set of distinct scene moves needed to satisfy the act contract.
  - Choose scene count from that movement, not from habit. `scene_count_guidance` is a soft range, not a quota.
  - Assign each claim one main home scene using `primary_claim_ids`. If later scenes reuse it, put it in `callback_claim_ids` as established context, not as a fresh explanation.
  - `primary_claim_ids` should be unique within the act. `callback_claim_ids` should refer only to material already established earlier in the act.
  - For every scene, think in listener terms: what the listener knows before, what must become clear, what the scene must not assume, and which new terms need explicit introduction.
  - Only then write beat cards.
- Scene design:
  - Every scene must change something: the listener should leave it with a sharper question, clearer model, new pressure, better contrast, or landed consequence.
  - Neighboring scenes must do different listener work. If two scenes land the same insight, merge them, delete one, or change one to a genuinely new move.
  - It is better to return fewer scenes than to keep a weak, repetitive, or purely connective scene.
  - Keep one clear job per scene. Split only when the listener would feel a meaningful shift.
  - Same-topic progression is good only when each scene adds a new layer such as reveal, apply, stress-test, cost, exception, comparison, consequence, or reframe.
  - A scene is not good enough if it sounds plausible but a non-expert listener would still not know who acted, what changed, or why it follows from the previous beat.
  - Do not default to `hook -> explanation-heavy middle -> takeaway`. Let the act contract determine whether the opening move is a hook, explanation, case study, or debate, and whether the closing move is a takeaway, explanation, debate, case study, or open question.
  - Do not restate the act's main reveal in the next scene as if it were new.
  - Not every claim deserves its own beat. Near-paraphrase claims or one mechanism family often belong in one scene, with the rest serving as support.
  - `prior_acts_primary_claims` lists material already established earlier. If it recurs here, it should usually return as context rather than as a fresh explanation unless this act adds a genuinely new layer.
  - `recent_act_structures` summarizes how the latest acts were shaped structurally. Use it to avoid repeating the same opening move, explanation-heavy middle, and ending function in back-to-back acts.
  - A genuinely new layer means the listener learns something meaningfully different, not just the same mechanism in different words.
  - In historical acts, keep the scene order chronologically legible unless the beat card clearly frames a later point as hindsight.
  - `case_study` is optional. Use it only when a concrete episode materially changes the listener's understanding, not when it simply replays an earlier image.
  - `debate` is optional. Use it only when the act has real conflict pressure (`conflict_count > 0`) or a true comparative argument.
  - `takeaway` and `summary` scenes should land implication or reframing, not mechanically recap the act.
  - Structural variety matters across the episode. If the previous act already used the same opening type, explanation-heavy middle, and takeaway cadence, choose a different act shape here instead of paraphrasing the same movement.
- Scene grounding:
  - Each scene description must be substantively driven by its assigned claims.
  - A scene may draw a consequence from its claims and already-established act context.
  - Do not jump to a broader thesis that the scene's claims do not support.
- Beat card discipline:
  - Prefer compact beat cards, not mini-script paragraphs. `description` should describe the beat, not write the scene. `notes` should stay short.
  - Each scene description must stay a beat card: 1-3 sentences, maximum 250 characters.
  - State what CHANGES in this scene: the shift in understanding, the new complication, or the consequence.
  - Use `listener_state_before`, `listener_state_after`, `must_make_clear`, `must_not_assume`, and `new_terms_to_introduce` as compact planning metadata, not prose decoration.
  - Do not draft dialogue, host banter, or narration inside `description`.
  - If a description needs more than 3 sentences or more than 250 characters, the scene is trying to do too much. Split it or simplify it.
- Evidence:
  - When a scene uses a claim, attach one or more `evidence_ids` from that claim's bundled support whenever available.
  - `primary_claim_ids`, `callback_claim_ids`, and `evidence_ids` are opaque labels. Reuse only labels present in the input and copy them exactly.
  - Prefer bundled claim support over improvising unsupported scenes or assuming an evidence search will rescue the beat later.
- Use `scene_job` to declare the move each callback-heavy scene is making:
  - `reframe`: reinterpret already established material
  - `cost`: show the tradeoff, sacrifice, or price of the system
  - `contrast`: sharpen difference against another path or expectation
  - `bridge`: pivot cleanly into the next beat or act
  - `synthesize`: land the shift through this scene's own material — show why what the listener just heard means something different or bigger than they thought. Do not restate earlier scenes' conclusions
  - `apply`: show how an established mechanism operates in a concrete case
  - `stress_test`: push an established mechanism under pressure
- `apply` scenes should begin from the concrete condition or operating case, not by replaying the full setup chain.
- `stress_test` scenes should begin from the pressure, failure mode, or attack, not by reintroducing the base mechanism from zero.
- `synthesize` scenes should land the shift through their own claims — show why what the listener just heard means something different or bigger than they thought. Do not restate earlier scenes' conclusions. They may callback to the prior mechanism briefly, but should spend most of the beat on implication or editorial landing.
- Use `cost` only when the scene is genuinely about what is lost, constrained, or paid for. Do not label an origin story or general payoff scene as `cost` unless the beat explicitly centers on tradeoff.
- Callback-only ending scenes should usually use `reframe`, `cost`, `contrast`, `bridge`, or `synthesize`.
- Callback-only ending scenes should stay compact and focused. They should not become oversized recap blocks.
- When writing a callback-heavy or ending beat card, lead with the new move first: the consequence, reframing, contrast, bridge, or cost. Do not lead by re-explaining the old mechanism.
- If a scene uses an established mechanism as context, mention that context briefly and then move immediately to what is new in this scene.
- A `contrast` scene should usually have one dominant outside comparator. Do not import several unrelated external mini-histories unless they clearly ladder up to one comparison point.
- If `prior_acts_ending_types` is non-empty, it lists the scene types that previous acts used for their final scene. Prefer a different closing scene type unless the material genuinely demands the same one.
- Let `story_role` shape the act:
  - `setup`: establish the paradox, world, and stakes with only the minimum mechanism needed to orient the listener
  - `complicate`: intensify the problem or institutional tension
  - `explain`: answer the act question with the minimum mechanism needed
  - `resolve`: show how the tension settles in practice
  - `payoff`: widen the lens and land why this matters
- The first scene should earn attention; the last scene should change the listener's understanding or leave a deliberate question."""


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

        if not isinstance(manifest.acts_json, list):
            raise RuntimeError("Act planner expected acts_json list in manifest")

        # JSONB nested in-place mutation is not tracked reliably by SQLAlchemy.
        # Work on a detached copy and assign it back to guarantee persistence.
        acts = copy.deepcopy(manifest.acts_json)
        total_scene_count = 0
        question_context_map = build_question_context_map(manifest.research_tree_json)
        claim_context_cache: dict[str, dict[str, Any]] = {}

        for i, act in enumerate(acts, start=1):
            if not isinstance(act, dict):
                logger.warning("Skipping invalid act payload type: %s", type(act).__name__)
                continue

            act_num = _positive_int(act.get("act_number"), i)
            act["act_number"] = act_num
            publish_event(run_id, step_started("phase5_planning", f"plan_act_{act_num}", f"Planning Act {act_num}"))

            # Load claims for this act
            raw_claim_ids = _uuid_list(act.get("claim_ids"))
            claim_contexts, evidence_contexts = await load_planning_context(
                db=db,
                claim_ids=raw_claim_ids,
                generation_run_id=state["generation_run_id"],
                question_context_map=question_context_map,
            )
            claim_context_cache.update(claim_contexts)
            act_claims = [claim_contexts[cid] for cid in raw_claim_ids if cid in claim_contexts]
            act_evidence = list(evidence_contexts.values())
            prior_acts_primary_claims = _collect_prior_acts_primary_claims(
                acts=acts,
                current_act_idx=i,
                claim_context_cache=claim_context_cache,
            )
            prior_acts_ending_types = _collect_prior_acts_ending_types(acts, i - 1)
            recent_act_structures = _collect_prior_act_structures(acts, i - 1)

            allowed_claim_ids = {c["id"] for c in act_claims}
            allowed_evidence_ids = {e["id"] for e in act_evidence}
            per_scene_duration = max(
                30,
                _positive_int(act.get("estimated_duration_seconds"), 300) // 6,
            )

            claim_sid = ShortIdMap(allowed_claim_ids, prefix="c")
            evidence_sid = ShortIdMap(allowed_evidence_ids, prefix="e")

            base_prompt = _build_act_prompt_payload(
                act=act,
                act_number=act_num,
                act_claims=act_claims,
                act_evidence=act_evidence,
                output_language=state.get("language", "en"),
                prior_acts_primary_claims=prior_acts_primary_claims,
                prior_acts_ending_types=prior_acts_ending_types,
                recent_act_structures=recent_act_structures,
            )
            prompt_claims = list(base_prompt["claims"])
            claim_support_lookup = _claim_support_lookup(prompt_claims)
            claim_statements_lookup: dict[str, str] = {
                c["id"]: c.get("statement", "")
                for c in act_claims
                if isinstance(c, dict) and isinstance(c.get("id"), str)
            }

            # Shorten LLM-facing IDs to reduce prompt waste and UUID typos.
            base_prompt["claims"] = _shorten_act_prompt_claims(
                prompt_claims,
                claim_sid=claim_sid,
                evidence_sid=evidence_sid,
            )
            prompt_allowed_claim_ids = allowed_claim_ids | claim_sid.short_ids
            prompt_allowed_evidence_ids = allowed_evidence_ids | evidence_sid.short_ids

            beat_sheet: list[dict[str, Any]] = []
            scene_strategy = ""
            last_error = ""
            quality_issues: list[dict[str, Any]] = []
            beat_sheet_valid = False
            failed_beat_sheet: list[dict[str, Any]] | None = None
            attempt_debugs: list[dict[str, Any]] = []
            request_failures = 0
            quality_failures = 0
            total_attempts = 0
            last_failure_status = ""
            retry_feedback_error = ""
            _dump_act_planner_debug(
                run_id=run_id,
                act_number=act_num,
                status="started",
                base_prompt=base_prompt,
                attempts=[],
                beat_sheet=None,
                gap_fill_result=None,
                quality_issues=[],
            )
            while total_attempts < ACT_PLANNER_MAX_TOTAL_ATTEMPTS:
                if request_failures >= ACT_PLANNER_REQUEST_RETRIES:
                    break
                if quality_failures >= ACT_PLANNER_QUALITY_RETRIES:
                    break
                total_attempts += 1
                prompt_payload = dict(base_prompt)
                if retry_feedback_error:
                    prompt_payload["strict_output_contract"] = (
                        "Return valid JSON with top-level keys scene_strategy and beat_sheet. No prose."
                    )
                    prompt_payload["previous_error"] = retry_feedback_error

                prompt = json.dumps(prompt_payload, ensure_ascii=False)
                raw = ""
                beat_data = None
                candidate_beat_sheet: list[dict[str, Any]] = []
                candidate_scene_strategy = ""
                candidate_quality_issues: list[dict[str, Any]] = []
                structural_quality_failure = False
                try:
                    logger.info(
                        "Act planner calling Sonnet for act %s (attempt %s, request_failures=%s/%s, quality_failures=%s/%s, prompt_chars=%s)",
                        act_num,
                        total_attempts,
                        request_failures,
                        ACT_PLANNER_REQUEST_RETRIES,
                        quality_failures,
                        ACT_PLANNER_QUALITY_RETRIES,
                        len(prompt),
                    )
                    raw = await call_sonnet_structured(system=ACT_PLANNER_SYSTEM, prompt=prompt, temperature=0.5)
                    beat_data = parse_llm_json(raw)
                    candidate_scene_strategy = _extract_scene_strategy(beat_data)
                    candidate_beat_sheet = _normalize_beat_sheet(
                        beat_data,
                        allowed_claim_ids=prompt_allowed_claim_ids,
                        allowed_evidence_ids=prompt_allowed_evidence_ids,
                        default_duration=per_scene_duration,
                    )
                    # Restore short IDs back to canonical UUIDs
                    for _scene in candidate_beat_sheet:
                        _scene["claim_ids"] = restore_id_list(_scene.get("claim_ids"), claim_sid)
                        _scene["primary_claim_ids"] = restore_id_list(_scene.get("primary_claim_ids"), claim_sid)
                        _scene["callback_claim_ids"] = restore_id_list(_scene.get("callback_claim_ids"), claim_sid)
                        _scene["evidence_ids"] = evidence_sid.restore_list(_scene.get("evidence_ids", []))
                    candidate_beat_sheet = _split_scene_claim_roles(candidate_beat_sheet)
                    candidate_beat_sheet = _assign_scene_jobs(candidate_beat_sheet)
                    candidate_quality_issues = await _collect_beat_sheet_quality_issues(
                        candidate_beat_sheet,
                        claim_statements=claim_statements_lookup,
                    )
                    structural_quality_failure = _has_structural_overlap_failures(candidate_quality_issues)
                    if candidate_beat_sheet:
                        if structural_quality_failure:
                            raise ValueError("beat_sheet failed structural beat validation after claim-role normalization")
                        attempt_debugs.append(
                            {
                                "attempt": total_attempts,
                                "status": "ok",
                                "prompt_payload": prompt_payload,
                                "llm_output": {
                                    "raw_chars": len(raw or ""),
                                    "raw": raw,
                                    "parsed_type": type(beat_data).__name__ if beat_data is not None else None,
                                    "parsed": beat_data,
                                },
                                "normalized_scene_count": len(candidate_beat_sheet),
                                "quality_issues": candidate_quality_issues,
                                "structural_quality_failure": structural_quality_failure,
                            }
                        )
                        beat_sheet = candidate_beat_sheet
                        scene_strategy = candidate_scene_strategy
                        quality_issues = candidate_quality_issues
                        beat_sheet_valid = True
                        break
                    raise ValueError("beat_sheet is empty after normalization")
                except Exception as exc:
                    if candidate_beat_sheet:
                        failed_beat_sheet = candidate_beat_sheet
                    quality_issues = candidate_quality_issues
                    last_error = f"{type(exc).__name__}: {exc}"
                    if structural_quality_failure:
                        quality_failures += 1
                        last_failure_status = "quality_gate_failed"
                        structural_details = []
                        for qi in candidate_quality_issues:
                            if qi.get("severity") != "structural":
                                continue
                            detail = _format_structural_retry_detail(
                                qi,
                                beat_sheet=candidate_beat_sheet,
                                claim_statements_lookup=claim_statements_lookup,
                            )
                            structural_details.append(detail)
                        retry_feedback_error = (
                            f"{last_error}\n\nSpecific issues to fix:\n"
                            + "\n".join(structural_details)
                        ) if structural_details else last_error
                    else:
                        request_failures += 1
                        last_failure_status = "llm_or_normalize_failed"
                        retry_feedback_error = "" if isinstance(exc, TimeoutError) else last_error
                    attempt_debugs.append(
                        {
                            "attempt": total_attempts,
                            "status": last_failure_status,
                            "prompt_payload": prompt_payload,
                            "llm_output": {
                                "raw_chars": len(raw or ""),
                                "raw": raw,
                                "parsed_type": type(beat_data).__name__ if beat_data is not None else None,
                                "parsed": beat_data,
                            },
                            "quality_issues": quality_issues,
                            "error": last_error,
                        }
                    )
                    logger.warning(
                        "Act planning output invalid for act %s (attempt %s, request_failures=%s/%s, quality_failures=%s/%s): %s",
                        act_num,
                        total_attempts,
                        request_failures,
                        ACT_PLANNER_REQUEST_RETRIES,
                        quality_failures,
                        ACT_PLANNER_QUALITY_RETRIES,
                        last_error,
                        exc_info=True,
                    )

            if not beat_sheet_valid:
                _dump_act_planner_debug(
                    run_id=run_id,
                    act_number=act_num,
                    status=last_failure_status or ("quality_gate_failed" if failed_beat_sheet else "llm_or_normalize_failed"),
                    base_prompt=base_prompt,
                    attempts=attempt_debugs,
                    beat_sheet=failed_beat_sheet,
                    gap_fill_result=None,
                    quality_issues=quality_issues,
                )
                raise RuntimeError(
                    f"Act planner failed after {total_attempts} attempts for act {act_num}. "
                    f"Request failures: {request_failures}/{ACT_PLANNER_REQUEST_RETRIES}. "
                    f"Quality failures: {quality_failures}/{ACT_PLANNER_QUALITY_RETRIES}. "
                    f"Last error: {last_error}"
                )

            if scene_strategy:
                act["scene_strategy"] = scene_strategy
            else:
                act.pop("scene_strategy", None)

            hydrated_existing_evidence_ids = _hydrate_scene_evidence_ids_from_claim_support(
                beat_sheet,
                claim_support_lookup,
            )

            _dump_act_planner_debug(
                run_id=run_id,
                act_number=act_num,
                status="ok",
                base_prompt=base_prompt,
                attempts=attempt_debugs,
                beat_sheet=beat_sheet,
                gap_fill_result={
                    "hydrated_existing_evidence_ids": hydrated_existing_evidence_ids,
                    "hydrated_existing_evidence_count": len(hydrated_existing_evidence_ids),
                },
                quality_issues=quality_issues,
            )

            act["beat_sheet"] = beat_sheet

            scene_count = len(act.get("beat_sheet", []))
            total_scene_count += scene_count
            publish_event(run_id, act_planned(act_num, scene_count))
            publish_event(run_id, step_completed("phase5_planning", f"plan_act_{act_num}"))

        if total_scene_count == 0:
            raise RuntimeError("Act planner produced no scenes across all acts")

        manifest.acts_json = acts
        await db.commit()

    return {"manifest_id": state["manifest_id"], "current_phase": "phase5_planning"}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _compact_scene_meta_text(value: Any, *, max_chars: int = MAX_SCENE_CLARITY_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    compact = " ".join(value.strip().split())
    if not compact:
        return ""
    return compact[:max_chars]


def _compact_scene_meta_list(
    value: Any,
    *,
    max_items: int = MAX_SCENE_CLARITY_ITEMS,
    max_item_chars: int = MAX_SCENE_CLARITY_ITEM_CHARS,
) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        compact = " ".join(item.strip().split())
        if not compact:
            continue
        compact = compact[:max_item_chars]
        key = compact.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(compact)
        if len(out) >= max_items:
            break
    return out


def _scene_count_bucket(scene_count: int) -> str:
    if scene_count <= 4:
        return "short"
    if scene_count <= 6:
        return "medium"
    return "long"


def _ratio_bucket(count: int, total: int) -> str:
    if total <= 0:
        return "none"
    ratio = count / total
    if ratio < 0.34:
        return "low"
    if ratio < 0.67:
        return "medium"
    return "high"


def _uuid_list(value: Any) -> list[str]:
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


def _recover_scene_claim_ids(
    raw_value: Any,
    allowed_claim_ids: set[str],
    scene_number: int,
) -> list[str]:
    """Extract valid claim_ids, allowing either canonical UUIDs or pre-approved short IDs."""
    if not isinstance(raw_value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item:
            continue
        if allowed_claim_ids and item in allowed_claim_ids:
            if item not in seen:
                seen.add(item)
                out.append(item)
            continue
        try:
            uuid.UUID(item)
        except ValueError:
            logger.warning("Scene %s: dropped malformed claim_id %r", scene_number, item)
            continue
        if allowed_claim_ids and item not in allowed_claim_ids:
            logger.warning("Scene %s: dropped unknown claim_id %r", scene_number, item)
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _recover_scene_claim_roles(
    scene: dict[str, Any],
    *,
    allowed_claim_ids: set[str],
    scene_number: int,
) -> tuple[list[str], list[str], bool]:
    raw_primary = scene.get("primary_claim_ids")
    raw_callback = scene.get("callback_claim_ids")
    has_explicit_roles = isinstance(raw_primary, list) or isinstance(raw_callback, list)

    if has_explicit_roles:
        primary_claim_ids = _recover_scene_claim_ids(raw_primary, allowed_claim_ids, scene_number)
        callback_claim_ids = _recover_scene_claim_ids(raw_callback, allowed_claim_ids, scene_number)
        primary_set = set(primary_claim_ids)
        callback_claim_ids = [cid for cid in callback_claim_ids if cid not in primary_set]
        return primary_claim_ids, callback_claim_ids, True

    return _recover_scene_claim_ids(scene.get("claim_ids"), allowed_claim_ids, scene_number), [], False


def _recover_scene_evidence_ids(
    raw_value: Any,
    allowed_evidence_ids: set[str],
    scene_number: int,
) -> list[str]:
    """Extract valid evidence_ids, allowing either canonical UUIDs or pre-approved short IDs."""
    if not isinstance(raw_value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item:
            continue
        if allowed_evidence_ids and item in allowed_evidence_ids:
            if item not in seen:
                seen.add(item)
                out.append(item)
            continue
        try:
            uuid.UUID(item)
        except ValueError:
            logger.warning("Scene %s: dropped malformed evidence_id %r", scene_number, item)
            continue
        if allowed_evidence_ids and item not in allowed_evidence_ids:
            logger.warning("Scene %s: dropped unknown evidence_id %r", scene_number, item)
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _extract_raw_scenes(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("beat_sheet", "scenes", "beats"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def _extract_scene_strategy(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get("scene_strategy")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _build_act_prompt_payload(
    *,
    act: dict[str, Any],
    act_number: int,
    act_claims: list[dict[str, Any]],
    act_evidence: list[dict[str, Any]],
    output_language: str,
    prior_acts_primary_claims: list[dict[str, Any]],
    prior_acts_ending_types: list[str] | None = None,
    recent_act_structures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    estimated_duration = _positive_int(act.get("estimated_duration_seconds"), 300)
    conflict_ids = _uuid_list(act.get("conflict_brief_ids"))
    prompt_claims = _build_claim_prompt_payloads(
        act_claims=act_claims,
        act_evidence=act_evidence,
        max_support_evidence=MAX_SUPPORT_EVIDENCE_PER_ACT,
    )
    return {
        "act_number": act_number,
        "output_language": str(output_language or "en").strip() or "en",
        "story_role": str(act.get("story_role") or "").strip(),
        "act_title": act.get("title", ""),
        "purpose": str(act.get("purpose") or "").strip(),
        "internal_arc": str(act.get("internal_arc") or "").strip(),
        "opening_question": str(act.get("opening_question") or "").strip(),
        "ending_turn": str(act.get("ending_turn") or "").strip(),
        "estimated_duration_seconds": estimated_duration,
        "scene_count_guidance": _scene_count_guidance(estimated_duration),
        "conflict_count": len(conflict_ids),
        "prior_acts_primary_claims": prior_acts_primary_claims,
        "prior_acts_ending_types": prior_acts_ending_types or [],
        "recent_act_structures": recent_act_structures or [],
        "claims": prompt_claims,
    }


def _collect_prior_acts_ending_types(
    acts: list[dict[str, Any]],
    current_act_idx: int,
) -> list[str]:
    """Return the scene type of the last scene in each prior act's beat_sheet."""
    ending_types: list[str] = []
    for i in range(current_act_idx):
        beat_sheet = acts[i].get("beat_sheet") or []
        if beat_sheet:
            last_scene = beat_sheet[-1]
            scene_type = str(last_scene.get("type") or "").strip()
            if scene_type:
                ending_types.append(scene_type)
    return ending_types


def _collect_prior_acts_primary_claims(
    *,
    acts: list[dict[str, Any]],
    current_act_idx: int,
    claim_context_cache: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for act in acts[: max(current_act_idx - 1, 0)]:
        if not isinstance(act, dict):
            continue
        act_number = _positive_int(act.get("act_number"), 0)
        beat_sheet = act.get("beat_sheet")
        if not isinstance(beat_sheet, list):
            continue
        for scene in beat_sheet:
            if not isinstance(scene, dict):
                continue
            for claim_id in _scene_primary_claim_ids(scene):
                if claim_id in seen:
                    continue
                claim_payload = claim_context_cache.get(claim_id)
                if not isinstance(claim_payload, dict):
                    continue
                statement = str(claim_payload.get("statement") or "").strip()
                if not statement:
                    continue
                seen.add(claim_id)
                ordered.append(
                    {
                        "statement": statement[:PRIOR_PRIMARY_STATEMENT_CHARS],
                        "act_number": act_number,
                    }
                )
    if len(ordered) <= MAX_PRIOR_PRIMARY_CLAIMS:
        return ordered
    return ordered[-MAX_PRIOR_PRIMARY_CLAIMS:]


def _dominant_value(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return ""
    max_count = max(counts.values())
    for value in values:
        if value and counts.get(value) == max_count:
            return value
    return ""


def _act_structure_signature(
    beat_sheet: list[dict[str, Any]],
    *,
    act_number: int | None = None,
) -> dict[str, Any]:
    ordered_scenes = sorted(
        [scene for scene in beat_sheet if isinstance(scene, dict)],
        key=lambda scene: _positive_int(scene.get("scene"), 0),
    )
    if not ordered_scenes:
        return {
            "act_number": act_number,
            "scene_type_sequence": [],
            "scene_job_sequence": [],
            "scene_claim_mode_sequence": [],
            "opening_type": "",
            "opening_job": "",
            "ending_type": "",
            "ending_job": "",
            "scene_count": 0,
            "scene_count_bucket": "short",
            "explanation_ratio_bucket": "none",
            "case_study_ratio_bucket": "none",
            "callback_density_bucket": "none",
            "dominant_middle_type": "",
            "dominant_middle_job": "",
        }

    scene_types = [str(scene.get("type") or "").strip() for scene in ordered_scenes]
    scene_jobs = [_scene_job(scene) for scene in ordered_scenes]
    scene_claim_modes = [_scene_claim_mode(scene) for scene in ordered_scenes]
    middle_scenes = ordered_scenes[1:-1] if len(ordered_scenes) > 2 else ordered_scenes
    middle_types = [str(scene.get("type") or "").strip() for scene in middle_scenes]
    middle_jobs = [_scene_job(scene) for scene in middle_scenes]
    explanation_count = sum(1 for scene_type in scene_types if scene_type == "explanation")
    case_study_count = sum(1 for scene_type in scene_types if scene_type == "case_study")
    callback_scene_count = sum(1 for mode in scene_claim_modes if mode in {"mixed", "callback"})

    return {
        "act_number": act_number,
        "scene_type_sequence": scene_types,
        "scene_job_sequence": scene_jobs,
        "scene_claim_mode_sequence": scene_claim_modes,
        "opening_type": scene_types[0],
        "opening_job": scene_jobs[0],
        "ending_type": scene_types[-1],
        "ending_job": scene_jobs[-1],
        "scene_count": len(ordered_scenes),
        "scene_count_bucket": _scene_count_bucket(len(ordered_scenes)),
        "explanation_ratio_bucket": _ratio_bucket(explanation_count, len(ordered_scenes)),
        "case_study_ratio_bucket": _ratio_bucket(case_study_count, len(ordered_scenes)),
        "callback_density_bucket": _ratio_bucket(callback_scene_count, len(ordered_scenes)),
        "dominant_middle_type": _dominant_value(middle_types),
        "dominant_middle_job": _dominant_value(middle_jobs),
    }


def _collect_prior_act_structures(
    acts: list[dict[str, Any]],
    current_act_idx: int,
) -> list[dict[str, Any]]:
    structures: list[dict[str, Any]] = []
    for act in acts[: max(current_act_idx, 0)]:
        if not isinstance(act, dict):
            continue
        beat_sheet = act.get("beat_sheet")
        if not isinstance(beat_sheet, list) or not beat_sheet:
            continue
        structures.append(
            _act_structure_signature(
                beat_sheet,
                act_number=_positive_int(act.get("act_number"), 0) or None,
            )
        )
    if len(structures) <= MAX_RECENT_ACT_STRUCTURES:
        return structures
    return structures[-MAX_RECENT_ACT_STRUCTURES:]


def _build_claim_prompt_payloads(
    *,
    act_claims: list[dict[str, Any]],
    act_evidence: list[dict[str, Any]],
    max_support_evidence: int,
) -> list[dict[str, Any]]:
    evidence_by_claim: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for claim in act_claims:
        claim_id = claim.get("id")
        if isinstance(claim_id, str) and claim_id.strip():
            evidence_by_claim[claim_id] = {
                "direct": _rank_support_candidates(
                    act_evidence,
                    claim_id=claim_id,
                    mode="direct",
                ),
                "illustrative": _rank_support_candidates(
                    act_evidence,
                    claim_id=claim_id,
                    mode="illustrative",
                ),
            }

    prompt_claims: list[dict[str, Any]] = []
    support_slots_used = 0
    support_cap = max(max_support_evidence, len(act_claims))

    for claim in act_claims:
        claim_id = str(claim.get("id") or "").strip()
        candidates = evidence_by_claim.get(claim_id, {"direct": [], "illustrative": []})
        direct_bundle = candidates["direct"][:MAX_DIRECT_SUPPORT_PER_CLAIM]
        support_slots_used += len(direct_bundle)
        prompt_claims.append(
            {
                "id": claim_id,
                "type": str(claim.get("type") or "").strip(),
                "verification": str(claim.get("verification") or "").strip(),
                "statement": str(claim.get("statement") or "").strip(),
                "has_illustrative": bool(claim.get("has_illustrative")),
                "illustrative_evidence_count": _positive_int(claim.get("illustrative_evidence_count"), 0),
                "direct_support_count": _positive_int(claim.get("direct_support_count"), 0),
                "source_diversity": _positive_int(claim.get("source_diversity"), 0),
                "support_bundle": {
                    "direct": direct_bundle,
                    "illustrative": [],
                },
            }
        )

    for prompt_claim in prompt_claims:
        if support_slots_used >= support_cap:
            break
        claim_id = prompt_claim["id"]
        direct_ids = {item["id"] for item in prompt_claim["support_bundle"]["direct"]}
        illustrative_candidates = [
            item
            for item in evidence_by_claim.get(claim_id, {}).get("illustrative", [])
            if item["id"] not in direct_ids
        ][:MAX_ILLUSTRATIVE_SUPPORT_PER_CLAIM]
        if not illustrative_candidates:
            continue
        prompt_claim["support_bundle"]["illustrative"] = illustrative_candidates
        support_slots_used += len(illustrative_candidates)

    return prompt_claims


def _rank_support_candidates(
    act_evidence: list[dict[str, Any]],
    *,
    claim_id: str,
    mode: str,
) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for evidence in act_evidence:
        evidence_id = str(evidence.get("id") or "").strip()
        if not evidence_id or evidence_id in seen_ids:
            continue

        claim_refs = evidence.get("supporting_claim_ids") if mode == "direct" else evidence.get("illustrative_claim_ids")
        if claim_id not in (claim_refs or []):
            continue

        compact = {
            "id": evidence_id,
            "quote": str(evidence.get("quote") or "").strip(),
            "asset_type": str(evidence.get("asset_type") or "").strip(),
            "source_title": str(evidence.get("source_title") or "").strip(),
            "authority_type": str(evidence.get("authority_type") or "").strip(),
        }
        if not compact["quote"]:
            continue

        ranked.append((_support_score(evidence, mode=mode), compact))
        seen_ids.add(evidence_id)

    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [item for _, item in ranked]


def _support_score(evidence: dict[str, Any], *, mode: str) -> float:
    score = 0.0
    authority_type = str(evidence.get("authority_type") or "").strip()
    source_reliability = str(evidence.get("source_reliability") or "").strip()
    direct_or_contextual = set(evidence.get("direct_or_contextual") or [])
    narrative_score = float(evidence.get("narrative_score") or 0.0)
    asset_type = str(evidence.get("asset_type") or "").strip()

    if authority_type == "primary":
        score += 2.0
    elif authority_type == "secondary":
        score += 1.0

    if source_reliability == "high":
        score += 1.5
    elif source_reliability == "medium":
        score += 0.75

    if not bool(evidence.get("is_temporary")):
        score += 0.25

    if mode == "direct":
        if bool(evidence.get("supports_fact")):
            score += 3.0
        if "direct" in direct_or_contextual:
            score += 2.0
        elif "contextual" in direct_or_contextual:
            score += 0.5
        score += min(narrative_score, 1.0) * 0.25
    else:
        if bool(evidence.get("illustrative")):
            score += 2.5
        if asset_type in {"anecdote", "case", "contrast", "timeline"}:
            score += 1.0
        score += min(narrative_score, 1.0) * 2.0

    return score


def _claim_support_lookup(prompt_claims: list[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    lookup: dict[str, dict[str, list[str]]] = {}
    for claim in prompt_claims:
        claim_id = str(claim.get("id") or "").strip()
        if not claim_id:
            continue
        support_bundle = claim.get("support_bundle") or {}
        lookup[claim_id] = {
            "direct": _bundle_evidence_ids(support_bundle.get("direct")),
            "illustrative": _bundle_evidence_ids(support_bundle.get("illustrative")),
        }
    return lookup


def _shorten_act_prompt_claims(
    prompt_claims: list[dict[str, Any]],
    *,
    claim_sid: ShortIdMap,
    evidence_sid: ShortIdMap,
) -> list[dict[str, Any]]:
    shortened = shorten_claim_dicts(prompt_claims, claim_sid)
    for claim in shortened:
        if not isinstance(claim, dict):
            continue
        support_bundle = claim.get("support_bundle")
        if not isinstance(support_bundle, dict):
            continue
        for mode in ("direct", "illustrative"):
            items = support_bundle.get(mode)
            if not isinstance(items, list):
                continue
            support_bundle[mode] = [
                {**item, "id": evidence_sid.shorten(str(item.get("id")))}
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                else item
                for item in items
            ]
    return shortened


def _bundle_evidence_ids(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    evidence_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("id") or "").strip()
        if not evidence_id or evidence_id in seen:
            continue
        seen.add(evidence_id)
        evidence_ids.append(evidence_id)
    return evidence_ids


def _hydrate_scene_evidence_ids_from_claim_support(
    beat_sheet: list[dict[str, Any]],
    claim_support_lookup: dict[str, dict[str, list[str]]],
) -> list[str]:
    hydrated_ids: list[str] = []
    seen: set[str] = set()
    for scene in beat_sheet:
        existing_ids = _uuid_list(scene.get("evidence_ids"))
        primary_claim_ids = _scene_primary_claim_ids(scene)
        callback_claim_ids = _scene_callback_claim_ids(scene)
        scene["evidence_ids"] = existing_ids
        if not primary_claim_ids and not callback_claim_ids:
            continue

        scene_type = str(scene.get("type") or "").strip()
        preferred_modes = (
            ("illustrative", "direct")
            if scene_type in {"hook", "case_study", "transition"}
            else ("direct", "illustrative")
        )
        attached = list(existing_ids)
        attached_set = set(existing_ids)
        existing_set = set(existing_ids)
        if len(attached) >= MAX_EXISTING_EVIDENCE_IDS_PER_SCENE:
            continue
        claim_id_groups = [primary_claim_ids]
        if callback_claim_ids:
            claim_id_groups.append(callback_claim_ids)

        for mode in preferred_modes:
            for claim_ids in claim_id_groups:
                for claim_id in claim_ids:
                    for evidence_id in claim_support_lookup.get(claim_id, {}).get(mode, []):
                        if evidence_id in attached_set:
                            continue
                        attached.append(evidence_id)
                        attached_set.add(evidence_id)
                        if len(attached) >= MAX_EXISTING_EVIDENCE_IDS_PER_SCENE:
                            break
                    if len(attached) >= MAX_EXISTING_EVIDENCE_IDS_PER_SCENE:
                        break
                if len(attached) >= MAX_EXISTING_EVIDENCE_IDS_PER_SCENE:
                    break
            if len(attached) >= MAX_EXISTING_EVIDENCE_IDS_PER_SCENE:
                break

        if not attached:
            continue

        scene["evidence_ids"] = attached
        for evidence_id in attached:
            if evidence_id in existing_set or evidence_id in seen:
                continue
            seen.add(evidence_id)
            hydrated_ids.append(evidence_id)

    return hydrated_ids


def _scene_count_guidance(estimated_duration_seconds: int) -> dict[str, int]:
    if estimated_duration_seconds <= 210:
        return {"min_scenes": 3, "target_scenes": 4, "max_scenes": 5}
    if estimated_duration_seconds <= 360:
        return {"min_scenes": 4, "target_scenes": 5, "max_scenes": 6}
    if estimated_duration_seconds <= 480:
        return {"min_scenes": 5, "target_scenes": 6, "max_scenes": 7}
    return {"min_scenes": 6, "target_scenes": 7, "max_scenes": 8}


def _normalize_beat_sheet(
    beat_data: Any,
    allowed_claim_ids: set[str],
    allowed_evidence_ids: set[str],
    default_duration: int,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    used_scene_numbers: set[int] = set()
    for i, scene in enumerate(_extract_raw_scenes(beat_data), start=1):
        if not isinstance(scene, dict):
            continue

        description = scene.get("description")
        if not isinstance(description, str) or not description.strip():
            notes_fallback = scene.get("notes")
            if isinstance(notes_fallback, str) and notes_fallback.strip():
                description = notes_fallback.strip()
            else:
                continue

        scene_number = _next_available_scene_number(
            _positive_int(scene.get("scene"), i),
            used_scene_numbers,
        )
        used_scene_numbers.add(scene_number)
        scene_type = scene.get("type")
        if not isinstance(scene_type, str) or not scene_type.strip():
            scene_type = "explanation"

        primary_claim_ids, callback_claim_ids, has_explicit_claim_roles = _recover_scene_claim_roles(
            scene,
            allowed_claim_ids=allowed_claim_ids,
            scene_number=scene_number,
        )
        evidence_ids = _recover_scene_evidence_ids(
            scene.get("evidence_ids"),
            allowed_evidence_ids,
            scene_number,
        )
        duration = _positive_int(scene.get("estimated_duration_seconds"), default_duration)

        transition_note = scene.get("transition_note")
        if transition_note is not None and not isinstance(transition_note, str):
            transition_note = None

        notes = scene.get("notes")
        if not isinstance(notes, str):
            notes = ""

        normalized.append(
            {
                "scene": scene_number,
                "type": scene_type.strip(),
                "scene_job": _normalize_scene_job(scene.get("scene_job")),
                "description": description.strip(),
                "claim_ids": list(primary_claim_ids),
                "primary_claim_ids": primary_claim_ids,
                "callback_claim_ids": callback_claim_ids,
                "listener_state_before": _compact_scene_meta_text(scene.get("listener_state_before")),
                "listener_state_after": _compact_scene_meta_text(scene.get("listener_state_after")),
                "must_make_clear": _compact_scene_meta_list(scene.get("must_make_clear")),
                "must_not_assume": _compact_scene_meta_list(scene.get("must_not_assume")),
                "new_terms_to_introduce": _compact_scene_meta_list(scene.get("new_terms_to_introduce")),
                "required_bridge_from_previous_scene": _compact_scene_meta_text(
                    scene.get("required_bridge_from_previous_scene")
                ),
                "evidence_ids": evidence_ids,
                "estimated_duration_seconds": duration,
                "transition_note": transition_note,
                "notes": notes.strip(),
                "_claim_role_source": "explicit" if has_explicit_claim_roles else "legacy",
            }
        )

    return normalized


def _scene_primary_claim_ids(scene: dict[str, Any]) -> list[str]:
    primary_claim_ids = _uuid_list(scene.get("primary_claim_ids"))
    if primary_claim_ids:
        return primary_claim_ids
    return _uuid_list(scene.get("claim_ids"))


def _scene_callback_claim_ids(scene: dict[str, Any]) -> list[str]:
    return _uuid_list(scene.get("callback_claim_ids"))


def _scene_claim_mode(scene: dict[str, Any]) -> str:
    primary_claim_ids = _scene_primary_claim_ids(scene)
    callback_claim_ids = _scene_callback_claim_ids(scene)
    if primary_claim_ids and callback_claim_ids:
        return "mixed"
    if callback_claim_ids:
        return "callback"
    return "primary"


def _normalize_scene_job(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().lower()
    return cleaned if cleaned in SCENE_JOB_VALUES else ""


def _scene_job(scene: dict[str, Any]) -> str:
    explicit = _normalize_scene_job(scene.get("scene_job"))
    if explicit:
        return explicit

    scene_type = str(scene.get("type") or "").strip()
    scene_claim_mode = _scene_claim_mode(scene)
    if scene_claim_mode == "callback":
        if scene_type in {"takeaway", "summary"}:
            return "synthesize"
        if scene_type == "transition":
            return "bridge"
        if scene_type == "debate":
            return "contrast"
        return "apply"
    if scene_claim_mode == "mixed":
        if scene_type == "transition":
            return "bridge"
        if scene_type == "debate":
            return "contrast"
        if scene_type in {"takeaway", "summary"}:
            return "synthesize"
        return "apply"
    return ""


def _assign_scene_jobs(beat_sheet: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for scene in beat_sheet:
        scene_job = _scene_job(scene)
        if scene_job:
            scene["scene_job"] = scene_job
        else:
            scene.pop("scene_job", None)
    return beat_sheet


def _split_scene_claim_roles(beat_sheet: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_primary_claim_ids: set[str] = set()
    ordered_scenes = sorted(
        beat_sheet,
        key=lambda scene: _positive_int(scene.get("scene"), 0) if isinstance(scene, dict) else 0,
    )
    for scene in ordered_scenes:
        role_source = str(scene.pop("_claim_role_source", "legacy") or "legacy").strip()
        if role_source == "explicit":
            explicit_primary_claim_ids = _scene_primary_claim_ids(scene)
            explicit_callback_claim_ids = [
                claim_id
                for claim_id in _scene_callback_claim_ids(scene)
                if claim_id not in set(explicit_primary_claim_ids)
            ]
            callback_claim_ids: list[str] = []
            callback_seen: set[str] = set()
            primary_claim_ids = []
            primary_seen_in_scene: set[str] = set()
            for claim_id in explicit_primary_claim_ids:
                if claim_id in seen_primary_claim_ids:
                    if claim_id not in callback_seen:
                        callback_claim_ids.append(claim_id)
                        callback_seen.add(claim_id)
                    continue
                if claim_id in primary_seen_in_scene:
                    continue
                primary_claim_ids.append(claim_id)
                primary_seen_in_scene.add(claim_id)
            for claim_id in explicit_callback_claim_ids:
                if claim_id in primary_seen_in_scene or claim_id in seen_primary_claim_ids:
                    if claim_id not in callback_seen:
                        callback_claim_ids.append(claim_id)
                        callback_seen.add(claim_id)
                    continue
                primary_claim_ids.append(claim_id)
                primary_seen_in_scene.add(claim_id)
        else:
            original_claim_ids = _uuid_list(scene.get("claim_ids"))
            primary_claim_ids = []
            callback_claim_ids = []
            for claim_id in original_claim_ids:
                if claim_id in seen_primary_claim_ids:
                    callback_claim_ids.append(claim_id)
                    continue
                primary_claim_ids.append(claim_id)
        scene["primary_claim_ids"] = primary_claim_ids
        scene["callback_claim_ids"] = callback_claim_ids
        scene["claim_ids"] = primary_claim_ids
        seen_primary_claim_ids.update(primary_claim_ids)
    return beat_sheet


async def _collect_beat_sheet_quality_issues(
    beat_sheet: list[dict[str, Any]],
    claim_statements: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Collect deterministic quality issues + LLM-based grounding/overlap checks.

    Args:
        beat_sheet: normalized beat sheet with claim roles split.
        claim_statements: mapping from claim_id to statement text (for Haiku checks).
            If None, LLM-based checks are skipped (e.g. in tests).
    """
    issues: list[dict[str, Any]] = []
    explanation_like = {"explanation", "case_study", "hook"}
    ending_like = {"takeaway", "summary", "transition"}
    last_scene_number = len(beat_sheet)

    for idx, scene in enumerate(beat_sheet):
        scene_number = _positive_int(scene.get("scene"), idx + 1)
        scene_type = str(scene.get("type") or "").strip()
        primary_claim_ids = _scene_primary_claim_ids(scene)
        callback_claim_ids = _scene_callback_claim_ids(scene)
        scene_job = _scene_job(scene)
        is_last_scene = idx == (last_scene_number - 1)
        description = str(scene.get("description") or "").strip()

        if description and len(description) > MAX_BEAT_CARD_DESCRIPTION_CHARS:
            issues.append(
                {
                    "severity": "structural",
                    "type": "beat_card_description_too_long",
                    "scene": scene_number,
                    "scene_type": scene_type,
                    "message": "Scene description exceeds the beat-card budget and may drift into mini-draft narration.",
                    "description_chars": len(description),
                }
            )

        if scene_type in explanation_like and not primary_claim_ids and callback_claim_ids:
            issues.append(
                {
                    "severity": "structural",
                    "type": "callback_only_scene",
                    "scene": scene_number,
                    "scene_type": scene_type,
                    "message": "Scene only references already-covered claims and introduces no primary claim.",
                    "callback_claim_ids": callback_claim_ids,
                }
            )
        elif is_last_scene and scene_type in ending_like and not primary_claim_ids and callback_claim_ids:
            issues.append(
                {
                    "severity": "advisory",
                    "type": "callback_only_ending",
                    "scene": scene_number,
                    "scene_type": scene_type,
                    "message": "Ending scene is callback-only; ensure it lands a new consequence or reframing instead of reteaching mechanism.",
                    "callback_claim_ids": callback_claim_ids,
                }
            )
            if len(callback_claim_ids) > MAX_CALLBACK_ENDING_CALLBACK_CLAIMS:
                issues.append(
                    {
                        "severity": "advisory",
                        "type": "callback_only_ending_too_many_claims",
                        "scene": scene_number,
                        "scene_type": scene_type,
                        "message": "Callback-only ending carries too many callback claims to land cleanly.",
                        "callback_claim_ids": callback_claim_ids,
                        "scene_job": scene_job,
                    }
                )
            if len(description) > MAX_CALLBACK_ENDING_DESCRIPTION_CHARS:
                issues.append(
                    {
                        "severity": "advisory",
                        "type": "callback_only_ending_too_long",
                        "scene": scene_number,
                        "scene_type": scene_type,
                        "message": "Callback-only ending description is too long and likely to become a recap block.",
                        "description_chars": len(description),
                        "scene_job": scene_job,
                    }
                )
            if scene_job and scene_job not in ENDING_SCENE_JOB_VALUES:
                issues.append(
                    {
                        "severity": "structural",
                        "type": "callback_only_ending_bad_scene_job",
                        "scene": scene_number,
                        "scene_type": scene_type,
                        "message": "Callback-only ending must use a landing job, not an introducing/apply job.",
                        "scene_job": scene_job,
                    }
                )

    # LLM-based quality checks (Haiku) — claim-description grounding + cross-scene insight dedup
    if claim_statements and len(beat_sheet) >= 2:
        llm_issues = await _run_llm_quality_checks(beat_sheet, claim_statements)
        issues.extend(llm_issues)

    return issues


async def _run_llm_quality_checks(
    beat_sheet: list[dict[str, Any]],
    claim_statements: dict[str, str],
) -> list[dict[str, Any]]:
    """Run Haiku-based grounding and overlap checks in parallel."""
    scenes_for_check: list[dict[str, Any]] = []
    for idx, scene in enumerate(beat_sheet):
        scene_number = _positive_int(scene.get("scene"), idx + 1)
        description = str(scene.get("description") or "").strip()
        primary_claim_ids = _scene_primary_claim_ids(scene)
        callback_claim_ids = _scene_callback_claim_ids(scene)
        primary_claim_stmts = [
            claim_statements[cid]
            for cid in dict.fromkeys(primary_claim_ids)
            if cid in claim_statements
        ]
        callback_claim_stmts = [
            claim_statements[cid]
            for cid in dict.fromkeys(callback_claim_ids)
            if cid in claim_statements
        ]
        scene_type = str(scene.get("type") or "").strip()
        # takeaway/summary scenes synthesize the whole act — grounding check
        # would compare their description against 1-2 leftover claims, producing
        # false positives.  Still include them for overlap check.
        skip_grounding = scene_type in {"takeaway", "summary"}
        if description and (primary_claim_stmts or callback_claim_stmts):
            scenes_for_check.append({
                "scene": scene_number,
                "type": scene_type,
                "description": description,
                "primary_claims": primary_claim_stmts,
                "callback_claims": callback_claim_stmts,
                "skip_grounding": skip_grounding,
            })

    if len(scenes_for_check) < 2:
        return []

    grounding_task = _check_description_claim_grounding(scenes_for_check)
    overlap_task = _check_cross_scene_overlap(scenes_for_check)
    grounding_issues, overlap_issues = await asyncio.gather(
        grounding_task, overlap_task, return_exceptions=True,
    )

    # Re-raise StepExecutionStopped so user stop requests propagate cleanly.
    for result in (grounding_issues, overlap_issues):
        if isinstance(result, StepExecutionStopped):
            raise result

    issues: list[dict[str, Any]] = []
    if isinstance(grounding_issues, list):
        issues.extend(grounding_issues)
    else:
        logger.warning("Haiku grounding check failed: %s", grounding_issues)
    if isinstance(overlap_issues, list):
        issues.extend(overlap_issues)
    else:
        logger.warning("Haiku overlap check failed: %s", overlap_issues)
    return issues


async def _check_description_claim_grounding(
    scenes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ask Haiku whether each scene's description is grounded in its claims."""
    # Skip takeaway/summary — they synthesize the whole act, not just their claims.
    groundable = [s for s in scenes if not s.get("skip_grounding")]
    if not groundable:
        return []
    prompt = json.dumps({
        "task": "For each scene, determine whether the description is substantively driven by the assigned claims. "
                "`primary_claims` are the scene's new material. `callback_claims` are previously established context the scene may legitimately rely on. "
                "A description is DISCONNECTED only if its core insight has NO basis in either the new material or the callback context — "
                "e.g. the description talks about a general thesis while both primary and callback claims are about unrelated specific facts. "
                "A description that explains, illustrates, or draws a consequence from primary claims plus callback context IS grounded — do NOT flag it. "
                "Do not treat callback-based context as a mismatch by itself. "
                "If in doubt, the scene is grounded. Return ONLY genuinely disconnected scenes.",
        "scenes": groundable,
        "return_format": {"disconnected_scenes": [{"scene": "scene_number", "reason": "why disconnected"}]},
    }, ensure_ascii=False)

    raw = await call_haiku_structured(
        system="You are a quality checker for podcast beat sheets. Return JSON only.",
        prompt=prompt,
        max_tokens=2048,
    )
    result = parse_llm_json(raw)
    issues: list[dict[str, Any]] = []
    if isinstance(result, dict):
        for item in result.get("disconnected_scenes", []):
            if not isinstance(item, dict):
                continue
            scene_num = item.get("scene")
            reason = item.get("reason", "")
            issues.append({
                "severity": "structural",
                "type": "description_claim_mismatch",
                "scene": scene_num,
                "message": f"Description is not grounded in scene claims: {reason}",
            })
    return issues


async def _check_cross_scene_overlap(
    scenes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ask Haiku whether any two scenes land the same insight."""
    prompt = json.dumps({
        "task": "Compare all scene pairs. Identify any two scenes whose descriptions land the same core insight, "
                "conclusion, or narrative move — even if worded differently. "
                "Two scenes overlap if a listener would feel they heard the same point twice. "
                "EXCEPTIONS — these are NOT overlap: "
                "(1) A `hook` scene teases a topic that a later `explanation` scene develops — that is progression, not repetition. "
                "(2) A `takeaway` or `summary` scene references material from earlier scenes to recontextualize or synthesize — that is its job. "
                "(3) Two scenes mention the same entity or fact but draw different conclusions or serve different narrative purposes. "
                "(4) Sequential scenes that deepen the SAME topic are progression: e.g. scene N introduces a mechanism, scene N+1 shows its broader system context, scene N+2 gives concrete numbers. They share a topic but each adds a new layer. This is good structure, not overlap. "
                "Only flag pairs where BOTH scenes land the SAME conclusion with NO added dimension — the listener would literally feel they heard the same point twice with nothing new.",
        "scenes": scenes,
        "return_format": {"overlapping_pairs": [{"scene_a": "number", "scene_b": "number", "shared_insight": "what overlaps"}]},
    }, ensure_ascii=False)

    raw = await call_haiku_structured(
        system="You are a quality checker for podcast beat sheets. Return JSON only.",
        prompt=prompt,
        max_tokens=2048,
    )
    result = parse_llm_json(raw)
    issues: list[dict[str, Any]] = []
    if isinstance(result, dict):
        for item in result.get("overlapping_pairs", []):
            if not isinstance(item, dict):
                continue
            scene_a = item.get("scene_a")
            scene_b = item.get("scene_b")
            shared = item.get("shared_insight", "")
            issues.append({
                "severity": "structural",
                "type": "description_overlap",
                "scene_a": scene_a,
                "scene_b": scene_b,
                "message": f"Scenes {scene_a} and {scene_b} land the same insight: {shared}. Rewrite one to use its own claims for a different move.",
            })
    return issues


def _has_structural_overlap_failures(issues: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(issue, dict) and issue.get("severity") == "structural"
        for issue in issues
    )


def _scene_claim_statements(
    scene: dict[str, Any],
    claim_statements_lookup: dict[str, str],
    *,
    include_callbacks: bool = False,
) -> list[str]:
    claim_ids = _scene_primary_claim_ids(scene)
    if include_callbacks:
        claim_ids += _scene_callback_claim_ids(scene)
    return [
        claim_statements_lookup.get(cid, "")
        for cid in dict.fromkeys(claim_ids)
        if cid in claim_statements_lookup
    ]


def _find_scene_by_number(
    beat_sheet: list[dict[str, Any]],
    scene_number: int,
) -> dict[str, Any] | None:
    for scene in beat_sheet:
        if _positive_int(scene.get("scene"), 0) == scene_number:
            return scene
    return None


def _format_structural_retry_detail(
    issue: dict[str, Any],
    *,
    beat_sheet: list[dict[str, Any]],
    claim_statements_lookup: dict[str, str],
) -> str:
    scene_key = issue.get("scene", issue.get("scene_a", "?"))
    detail = f"[S{scene_key}] {issue['type']}: {issue['message']}"

    if issue.get("type") == "description_claim_mismatch":
        scene_number = _positive_int(issue.get("scene"), 0)
        scene = _find_scene_by_number(beat_sheet, scene_number)
        if scene is None:
            return detail
        scene_claims = _scene_claim_statements(
            scene,
            claim_statements_lookup,
            include_callbacks=True,
        )
        if scene_claims:
            detail += "\n  Scene's actual claims and callback context:\n" + "\n".join(
                f"    - {statement}"
                for statement in scene_claims
            )
        return detail

    if issue.get("type") == "callback_only_scene":
        scene_number = _positive_int(issue.get("scene"), 0)
        scene = _find_scene_by_number(beat_sheet, scene_number)
        detail += (
            "\n  This scene has no new primary claim after claim-role normalization."
            "\n  Do NOT keep it just to preserve a template or to hit `scene_count_guidance`."
            "\n  Either:"
            "\n    - merge this material into the earlier scene that already owns these claims,"
            "\n    - replace it with a scene built from a different unused primary claim family, or"
            "\n    - delete it and return a shorter act."
        )
        if scene is not None:
            scene_job = _scene_job(scene) or "none"
            detail += f"\n  Scene {scene_number} current move: {scene_job}"
            callback_claims = _scene_callback_claim_ids(scene)
            scene_claims = [
                claim_statements_lookup.get(cid, "")
                for cid in callback_claims
                if cid in claim_statements_lookup
            ]
            if scene_claims:
                detail += "\n  Scene's callback claims:\n" + "\n".join(
                    f"    - {statement}"
                    for statement in scene_claims
                )
        return detail

    if issue.get("type") != "description_overlap":
        return detail

    scene_a_number = _positive_int(issue.get("scene_a"), 0)
    scene_b_number = _positive_int(issue.get("scene_b"), 0)
    scene_a = _find_scene_by_number(beat_sheet, scene_a_number)
    scene_b = _find_scene_by_number(beat_sheet, scene_b_number)

    detail += (
        "\n  Do NOT solve this by paraphrasing the same mechanism in new words."
        "\n  Keep one scene as the primary explanation. For the other scene, either:"
        "\n    - merge it into the stronger scene,"
        "\n    - demote it to a brief callback using `reframe`, `contrast`, `cost`, `bridge`, or `synthesize`, or"
        "\n    - rewrite it so it does genuinely different listener work such as `apply`, `stress_test`, consequence, tradeoff, exception, or comparator."
        "\n  If no distinct move exists, delete the weaker scene instead of repeating the mechanism."
    )

    for scene_number, scene in ((scene_a_number, scene_a), (scene_b_number, scene_b)):
        if scene is None:
            continue
        scene_job = _scene_job(scene) or "none"
        detail += f"\n  Scene {scene_number} current move: {scene_job}"
        scene_claims = _scene_claim_statements(scene, claim_statements_lookup)
        if scene_claims:
            detail += "\n  Scene {scene_number} claims:".format(scene_number=scene_number)
            detail += "\n" + "\n".join(f"    - {statement}" for statement in scene_claims)

    return detail


def _dump_act_planner_debug(
    *,
    run_id: str,
    act_number: int,
    status: str,
    base_prompt: dict,
    attempts: list[dict[str, Any]],
    beat_sheet: list[dict[str, Any]] | None,
    gap_fill_result: dict[str, Any] | None,
    quality_issues: list[dict[str, Any]] | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="planning_acts",
        filename_stem=(
            f"run-{safe_filename_token(run_id)}_"
            f"act-{act_number:02d}_{safe_filename_token(status)}"
        ),
        payload={
            "run_id": run_id,
            "act_number": act_number,
            "status": status,
            "input": base_prompt,
            "attempts": attempts,
            "normalized_beat_sheet": beat_sheet,
            "gap_fill_result": gap_fill_result,
            "quality_issues": quality_issues or [],
        },
    )


def _next_available_scene_number(candidate: int, used: set[int]) -> int:
    scene_number = candidate if candidate > 0 else 1
    while scene_number in used:
        scene_number += 1
    return scene_number
