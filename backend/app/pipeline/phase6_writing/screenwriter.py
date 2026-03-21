"""Phase 6.1: Screenwriter — generate dialogue per scene."""

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.show_bible import ShowBible
from app.models.claim import Claim
from app.models.evidence import EvidenceSpan
from app.models.episode_manifest import EpisodeManifest
from app.models.generation_run import GenerationRun
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.llm_json import parse_llm_json
from app.models.script_segment import ScriptSegment, AuditStatus
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, scene_written, progress

logger = logging.getLogger(__name__)
SCENE_GENERATION_ATTEMPTS = 3
SCENE_RETRY_BASE_DELAY_SECONDS = 1.0
RECENT_SCENE_STARTS_MAX_ITEMS = 5
PREVIOUS_SCENE_STATEMENT_CHARS = 200
SCENE_JOB_VALUES = {
    "reframe",
    "cost",
    "contrast",
    "bridge",
    "synthesize",
    "apply",
    "stress_test",
}

SCREENWRITER_SYSTEM = """You are a podcast screenwriter creating natural dialogue between two hosts.
Given a scene brief, local act context, claims, evidence, and host personas, write the dialogue.

Return JSON array:
[
  {
    "speaker": "A" or "B",
    "text": "dialogue line",
    "ssml_hints": {"break_after": "500ms", "emphasis": []},
    "emotion": "curious|confident|skeptical|surprised|amused|serious",
    "claim_ids": ["id"] or []
  }
]

Speech rules by verification level:
- VERIFIED → speak confidently with clear attribution to evidence.
- OBSERVED → include light uncertainty and attribution.
- UNVERIFIED → clearly mark uncertainty and avoid definitive claims.
- DISPUTED → present competing positions and why they differ.
- OPINION → explicit attribution to speaker/source.
- REPORT → recount with source context.

Host defaults:
- Host A often carries structure, narrative motion, or synthesis when helpful.
- Host B often pressure-tests, reframes, reacts, or spots what is missing.
- These are soft tendencies, not rigid scene roles.
- Either host may ask, infer, challenge, summarize, narrate, or land a point.

Writing rules:
- Language:
  - All dialogue lines MUST be in the `language` from the user payload.
  - Do not mix languages inside one line unless the evidence itself requires a short quoted term.
- Use `description` as the main job of the scene. Use `transition_note` only as a light landing/bridge if it helps the ending.
- Use `act_title`, `previous_scene`, `previous_act_summary`, `already_covered_claims`, `already_covered_claims_global`, and `new_information_goal` only as continuity context. Do not restate material that the listener already heard unless this scene is explicitly reframing or paying it off.
- For the first scene of a later act, use `previous_act_summary` to build on what the listener already knows instead of resetting the story from zero.
- If `previous_scene.last_line` is present, prefer opening the scene as a natural continuation of that last spoken thought when it fits the beat. Do not hard-reset the conversation if a smooth carry-over is available.
- The first 1-2 lines of a new scene should advance, challenge, narrow, or reframe what the listener just heard. Do not open by paraphrasing the previous scene's closing proposition unless the beat explicitly requires a callback.
- If the previous scene already announced the event, mechanism result, or punchline, start from the consequence, contradiction, pressure, cost, or next question instead of re-announcing it.
- If the current scene overlaps with covered local or global material, briefly refer back and move to what is new in this scene.
- If `already_covered_claims_global` includes `mechanism_anchors`, treat those anchors as signs that the mechanism is already established. Do not reteach it from zero unless this scene adds a new consequence, stress test, or reframing.
- Use `scene_claim_mode` to decide how much explanation is still allowed:
  - `primary`: introduce the new material clearly
  - `mixed`: explain the primary part, but treat `callback_claim_ids` as already established
  - `callback`: reference the established mechanism briefly and move to implication, cost, contrast, or bridge
- Use `scene_job` as the positive move this scene must make:
  - `reframe`: reinterpret already-established material
  - `cost`: show the price or tradeoff
  - `contrast`: sharpen difference against another path or expectation
  - `bridge`: pivot cleanly into the next beat or act
  - `synthesize`: compress what changed in understanding
  - `apply`: show an established mechanism operating in a concrete case
  - `stress_test`: push an established mechanism under pressure
- `callback_claim_ids` are not fresh exposition material. Do not turn callback-only scenes into mechanism walkthroughs.
- If `scene_claim_mode = callback`, let `scene_job` control the scene. Reference the established mechanism briefly, then spend the scene on the job.
- For `cost` scenes, open with the price, contradiction, or compromise being paid. Do not reopen by retelling the event beat that the previous scene just landed.
- Treat `new_information_goal` as the delta this scene must add. Do not spend most of the scene re-teaching already covered setup.
- Keep the scene moving. Do not let both hosts restate the same point in slightly different words.
- Avoid dry explanatory dialogue by default. Prefer a concrete opening move, a turn, and a landing.
- `recent_scene_starts` lists opener shapes already used in this act. If a recent opener shape is already present there, choose a different natural opening when possible.
- Let `scene_type` shape the scene:
  - `hook`: open with surprise, tension, or a vivid listener-facing question
  - `case_study`: ground the scene in a concrete event, example, or episode
  - `explanation`: clarify only the mechanism needed for this beat; do not drift into lecture mode
  - `debate`: surface real tension or tradeoff, not a fake argument
  - `transition`: pivot the listener cleanly from one framing to another
  - `summary`: compress what changed in understanding without sounding like bullet points
  - `takeaway`: land the consequence or implication, not just a generic recap
- Do not make Host A always explain and Host B always ask for proof in the same rhythm. Vary who initiates, who reframes, and who gets the last word.
- Do not default every scene to strict `ABAB` alternation. Occasional `AA` or `BB` runs are fine when they sound natural.
- Vary agreement, confirmation, and challenge shapes. Not every challenge needs an interruption-style opener, and not every agreement needs an explicit confirmation marker.
- A direct continuation, answer, or correction is often better than an explicit confirmation line. Do not make every clarification exchange sound like question -> confirmation marker -> explanation.
- A challenge can begin from tension, contrast, doubt, or a missing implication. It does not always need to sound like an interruption.
- Do not default every reaction to the same opener or confirmation shape. Vary discourse moves and avoid repeating one canned rhythm scene after scene.
- If a nearby scene already used the same opener shape or agreement shape, pick a different natural move unless repetition is clearly intentional.
- Use evidence organically. Do not dump raw quotes or source language into dialogue unless quoting is the point of the beat.
- When a precise figure, legal clause, institutional rule, or date matters, occasionally anchor it with light attribution such as "по данным центрального банка", "как сказано в законе", or "согласно официальным правилам фонда" instead of presenting every fact as unattributed narration.
- When claims are OBSERVED, UNVERIFIED, DISPUTED, OPINION, or REPORT, make the line sound appropriately attributed or qualified.
- Prefer callbacks over re-explanations. If a prior scene already established the mechanism, this scene should build on it rather than teach it from scratch again.

Keep replies under __MAX_REPLY_TOKENS__ tokens. Natural, conversational tone.
Treat host/policy fields as abstract constraints; do not copy their wording verbatim into dialogue.
Vary uncertainty wording naturally; avoid repeating one canned hedge phrase.
NEVER use any forbidden phrases."""


async def write_screenplay(state: PipelineState) -> dict:
    """LangGraph node: write dialogue for all acts sequentially."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase6_writing", "Writing podcast screenplay"))
    update_generation_progress(run_id, "phase6_writing", "screenwriter", 62.0)

    all_segment_ids: list[str] = []
    all_segment_ids_set: set[str] = set()
    planned_scene_count = 0
    total_dialogue_lines = 0

    async with async_session_factory() as db:
        run_uuid = uuid.UUID(run_id)
        run_result = await db.execute(select(GenerationRun).where(GenerationRun.id == run_uuid))
        run = run_result.scalar_one_or_none()
        force_rewrite_on_step_rerun = _should_force_rewrite_for_node_rerun(
            config=(run.config if run is not None else None),
            node_name="write_screenplay",
        )

        # Load ShowBible
        bible = None
        hosts = [{"name": "Host A", "role": "Guide"}, {"name": "Host B", "role": "Challenger"}]
        forbidden = []
        format_rules = {}
        if state.get("show_bible_id"):
            result = await db.execute(select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"])))
            bible = result.scalar_one_or_none()
            if bible:
                hosts = bible.hosts or hosts
                forbidden = bible.forbidden_phrases or []
                format_rules = bible.format_rules or {}

        # Load manifest
        result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
        )
        manifest = result.scalar_one_or_none()
        if not manifest or not manifest.acts_json:
            return {"script_segment_ids": [], "current_phase": "phase6_writing"}

        all_manifest_claim_ids = _collect_manifest_claim_ids(manifest.acts_json)
        global_claim_lookup = await _load_claim_prompt_payloads(db, all_manifest_claim_ids)

        try:
            max_tokens = int(format_rules.get("max_reply_tokens", 120))
        except (TypeError, ValueError):
            max_tokens = 120
        max_tokens = max(32, max_tokens)
        system_prompt = SCREENWRITER_SYSTEM.replace("__MAX_REPLY_TOKENS__", str(max_tokens))

        existing_result = await db.execute(
            select(ScriptSegment)
            .where(ScriptSegment.generation_run_id == run_uuid)
            .order_by(ScriptSegment.updated_at.desc(), ScriptSegment.created_at.desc())
        )
        existing_segments: dict[tuple[int, int], ScriptSegment] = {}
        duplicate_scene_keys: list[tuple[int, int]] = []
        for segment in existing_result.scalars():
            scene_key = (segment.act_number, segment.scene_number)
            if scene_key in existing_segments:
                duplicate_scene_keys.append(scene_key)
                continue
            existing_segments[scene_key] = segment

        if duplicate_scene_keys:
            logger.warning(
                "Found duplicate script segments for run %s; using latest rows for scenes: %s",
                run_id,
                duplicate_scene_keys[:20],
            )

        covered_claim_ids_global: list[str] = []
        covered_claim_ids_global_set: set[str] = set()

        for act_idx, act in enumerate(manifest.acts_json, start=1):
            act_num = act.get("act_number", 0)
            beat_sheet = act.get("beat_sheet", [])
            if not isinstance(beat_sheet, list):
                logger.warning("Act %s beat_sheet has invalid type: %s", act_num, type(beat_sheet).__name__)
                continue

            act_claim_ids: list[str] = []
            for act_scene in beat_sheet:
                if not isinstance(act_scene, dict):
                    continue
                for cid in _scene_manifest_claim_ids(act_scene):
                    if isinstance(cid, str) and cid not in act_claim_ids:
                        act_claim_ids.append(cid)
            act_claim_lookup = {
                cid: global_claim_lookup[cid]
                for cid in act_claim_ids
                if cid in global_claim_lookup
            }
            covered_claim_ids_in_act: list[str] = []
            covered_claim_ids_set: set[str] = set()
            covered_claim_ids_before_act = list(covered_claim_ids_global)
            previous_act_summary = _build_previous_act_summary(
                manifest.acts_json,
                act_idx,
                existing_segments=existing_segments,
                claim_lookup=global_claim_lookup,
            )

            seen_scene_numbers_in_act: set[int] = set()
            for scene_idx, scene in enumerate(beat_sheet, start=1):
                if not isinstance(scene, dict):
                    continue
                scene_num = _normalized_scene_number(scene.get("scene"), scene_idx)
                if scene_num in seen_scene_numbers_in_act:
                    logger.warning(
                        "Skipping duplicate scene number %s in act %s",
                        scene_num,
                        act_num,
                    )
                    continue
                seen_scene_numbers_in_act.add(scene_num)
                scene_key = (act_num, scene_num)
                existing_segment = existing_segments.get(scene_key)
                existing_line_count = (
                    _dialogue_line_count(existing_segment.dialogue_json)
                    if existing_segment is not None
                    else 0
                )
                should_rewrite_scene = (
                    existing_segment is not None
                    and (
                        existing_segment.audit_status == AuditStatus.REVISED
                        or force_rewrite_on_step_rerun
                    )
                )
                should_reuse_scene = (
                    existing_segment is not None
                    and not should_rewrite_scene
                    and existing_line_count > 0
                )
                planned_scene_count += 1
                if should_reuse_scene:
                    step_detail = f"Reusing Act {act_num}, Scene {scene_num} from prior checkpoint"
                elif should_rewrite_scene:
                    step_detail = f"Rewriting Act {act_num}, Scene {scene_num}"
                else:
                    step_detail = f"Writing Act {act_num}, Scene {scene_num}"
                publish_event(run_id, step_started("phase6_writing", f"write_{act_num}_{scene_num}", step_detail))

                if should_reuse_scene:
                    _append_unique_segment_id(str(existing_segment.id), all_segment_ids, all_segment_ids_set)
                    total_dialogue_lines += existing_line_count
                    _extend_unique_ids(
                        ordered=covered_claim_ids_in_act,
                        seen=covered_claim_ids_set,
                        new_values=_extract_dialogue_claim_ids(existing_segment.dialogue_json),
                    )
                    _extend_unique_ids(
                        ordered=covered_claim_ids_global,
                        seen=covered_claim_ids_global_set,
                        new_values=_extract_dialogue_claim_ids(existing_segment.dialogue_json),
                    )
                    publish_event(run_id, scene_written(act_num, scene_num))
                    publish_event(run_id, step_completed("phase6_writing", f"write_{act_num}_{scene_num}"))
                    continue

                # Gather claims and evidence for this scene
                scene_primary_claim_ids = _scene_primary_claim_ids(scene)
                scene_claims = [
                    act_claim_lookup[cid]
                    for cid in scene_primary_claim_ids
                    if isinstance(cid, str) and cid in act_claim_lookup
                ]

                scene_evidence = []
                for eid in scene.get("evidence_ids", []):
                    r = await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == uuid.UUID(eid)))
                    e = r.scalar_one_or_none()
                    if e:
                        scene_evidence.append({
                            "id": str(e.id), "quote": e.verbatim_quote[:300],
                            "asset_type": e.asset_type, "tellability": e.tellability_notes,
                        })

                prompt_payload = _build_scene_prompt_payload(
                    act=act,
                    act_number=act_num,
                    scene=scene,
                    scene_number=scene_num,
                    scene_claims=scene_claims,
                    scene_evidence=scene_evidence,
                    hosts=hosts,
                    forbidden_phrases=forbidden,
                    max_reply_tokens=max_tokens,
                    language=state["language"],
                    previous_scene=_build_previous_scene_payload(
                        beat_sheet,
                        scene_idx,
                        act_number=act_num,
                        existing_segments=existing_segments,
                        claim_lookup=act_claim_lookup,
                    ),
                    previous_act_summary=previous_act_summary if scene_idx == 1 else None,
                    already_covered_claims=_build_already_covered_claims_payload(
                        covered_claim_ids=covered_claim_ids_in_act,
                        claim_lookup=act_claim_lookup,
                    ),
                    already_covered_claims_global=_build_already_covered_claims_payload(
                        covered_claim_ids=covered_claim_ids_before_act,
                        claim_lookup=global_claim_lookup,
                        max_items=16,
                        statement_chars=160,
                        include_mechanism_anchors=True,
                    ),
                    recent_scene_starts=_build_recent_scene_starts(
                        beat_sheet,
                        scene_idx,
                        act_number=act_num,
                        existing_segments=existing_segments,
                    ),
                    new_information_goal=_build_new_information_goal(
                        scene=scene,
                        scene_claims=scene_claims,
                        local_covered_claim_ids=covered_claim_ids_set,
                        global_covered_claim_ids=covered_claim_ids_global_set,
                    ),
                )
                prompt = json.dumps(prompt_payload, ensure_ascii=False)

                dialogue = []
                last_error: Exception | None = None
                attempt_debugs: list[dict[str, Any]] = []
                for attempt in range(1, SCENE_GENERATION_ATTEMPTS + 1):
                    raw = ""
                    parsed = None
                    try:
                        raw = await call_sonnet_structured(
                            system=system_prompt,
                            prompt=prompt,
                            max_tokens=4096,
                            temperature=0.7,
                        )
                        parsed = parse_llm_json(raw)
                        if not isinstance(parsed, list):
                            raise ValueError(
                                f"screenwriter output must be a JSON array, got {type(parsed).__name__}"
                            )

                        dialogue = []
                        for item in parsed:
                            if not isinstance(item, dict):
                                continue
                            text = item.get("text")
                            if not isinstance(text, str) or not text.strip():
                                continue
                            line = dict(item)
                            line["speaker"] = _normalize_speaker_label(line.get("speaker"))
                            line["text"] = text.strip()
                            if not isinstance(line.get("ssml_hints"), dict):
                                line["ssml_hints"] = {}
                            if not isinstance(line.get("claim_ids"), list):
                                line["claim_ids"] = []
                            dialogue.append(line)

                        if not dialogue:
                            raise ValueError("screenwriter output contains no valid dialogue lines")
                        attempt_debugs.append(
                            {
                                "attempt": attempt,
                                "status": "ok",
                                "llm_output": {
                                    "raw_chars": len(raw or ""),
                                    "raw": raw,
                                    "parsed_type": type(parsed).__name__ if parsed is not None else None,
                                    "parsed": parsed,
                                },
                                "dialogue_line_count": len(dialogue),
                            }
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                        attempt_debugs.append(
                            {
                                "attempt": attempt,
                                "status": "invalid_output",
                                "llm_output": {
                                    "raw_chars": len(raw or ""),
                                    "raw": raw,
                                    "parsed_type": type(parsed).__name__ if parsed is not None else None,
                                    "parsed": parsed,
                                },
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        logger.warning(
                            "Screenwriting attempt %s/%s failed for act %s scene %s",
                            attempt,
                            SCENE_GENERATION_ATTEMPTS,
                            act_num,
                            scene_num,
                            exc_info=True,
                        )
                        if attempt < SCENE_GENERATION_ATTEMPTS:
                            await asyncio.sleep(SCENE_RETRY_BASE_DELAY_SECONDS * attempt)

                if not dialogue:
                    _dump_screenwriter_debug(
                        run_id=run_id,
                        act_number=act_num,
                        scene_number=scene_num,
                        status="llm_or_parse_failed",
                        prompt_payload=prompt_payload,
                        attempts=attempt_debugs,
                        dialogue=None,
                        error=f"{type(last_error).__name__}: {last_error}" if last_error else "unknown_error",
                    )
                    raise RuntimeError(
                        f"Screenwriting failed for act {act_num} scene {scene_num} "
                        f"after {SCENE_GENERATION_ATTEMPTS} attempts: {last_error}"
                    ) from last_error

                if existing_segment is None:
                    segment = ScriptSegment(
                        id=uuid.uuid4(),
                        generation_run_id=run_uuid,
                        act_number=act_num,
                        scene_number=scene_num,
                        dialogue_json=dialogue,
                    )
                    db.add(segment)
                    existing_segments[scene_key] = segment
                else:
                    segment = existing_segment
                    segment.dialogue_json = dialogue
                    segment.engagement_score = None
                    segment.audit_status = AuditStatus.PENDING
                    segment.quality_metrics = None

                await db.flush()
                await db.commit()
                _append_unique_segment_id(str(segment.id), all_segment_ids, all_segment_ids_set)
                total_dialogue_lines += len(dialogue)
                _extend_unique_ids(
                    ordered=covered_claim_ids_in_act,
                    seen=covered_claim_ids_set,
                    new_values=_extract_dialogue_claim_ids(dialogue),
                )
                _extend_unique_ids(
                    ordered=covered_claim_ids_global,
                    seen=covered_claim_ids_global_set,
                    new_values=_extract_dialogue_claim_ids(dialogue),
                )
                _dump_screenwriter_debug(
                    run_id=run_id,
                    act_number=act_num,
                    scene_number=scene_num,
                    status="ok",
                    prompt_payload=prompt_payload,
                    attempts=attempt_debugs,
                    dialogue=dialogue,
                    error=None,
                )

                publish_event(run_id, scene_written(act_num, scene_num))
                publish_event(run_id, step_completed("phase6_writing", f"write_{act_num}_{scene_num}"))

        if planned_scene_count == 0:
            raise RuntimeError("Screenwriter received an empty beat sheet; no scenes to write")
        if total_dialogue_lines == 0:
            raise RuntimeError("Screenwriter produced no dialogue lines")

    publish_event(run_id, progress(75.0, f"Wrote {len(all_segment_ids)} scenes"))
    update_generation_progress(run_id, "phase6_writing", "screenwriter_done", 75.0)

    return {"script_segment_ids": all_segment_ids, "current_phase": "phase6_writing"}


def _dialogue_line_count(dialogue_json: object) -> int:
    if not isinstance(dialogue_json, list):
        return 0
    count = 0
    for line in dialogue_json:
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if isinstance(text, str) and text.strip():
            count += 1
    return count


_SPEAKER_TRANSLATION = str.maketrans({
    "А": "A",
    "а": "a",
    "Б": "B",
    "б": "b",
})


def _normalize_speaker_label(value: object) -> str:
    if not isinstance(value, str):
        return "A"

    text = value.strip()
    if not text:
        return "A"

    translated = text.translate(_SPEAKER_TRANSLATION).upper()
    if translated in {"A", "B"}:
        return translated

    match = re.fullmatch(r"(?:HOST|SPEAKER)?\s*([AB])", translated)
    if match:
        return match.group(1)

    return "A"


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


def _should_force_rewrite_for_node_rerun(config: dict | None, *, node_name: str) -> bool:
    if not isinstance(config, dict):
        return False
    step_control = config.get("step_control")
    if not isinstance(step_control, dict):
        return False
    requested_action = step_control.get("requested_action")
    if requested_action != "rerun":
        return False
    requested_target_node = step_control.get("requested_target_node")
    if isinstance(requested_target_node, str) and requested_target_node.strip():
        return requested_target_node.strip() == node_name
    last_node = step_control.get("last_node")
    return isinstance(last_node, str) and last_node == node_name


def _append_unique_segment_id(segment_id: str, ordered: list[str], seen: set[str]) -> None:
    if segment_id in seen:
        return
    seen.add(segment_id)
    ordered.append(segment_id)


def _extend_unique_ids(*, ordered: list[str], seen: set[str], new_values: list[str]) -> None:
    for value in new_values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)


def _normalized_scene_number(value: object, fallback: int) -> int:
    try:
        scene_number = int(value)
    except (TypeError, ValueError):
        return fallback
    return scene_number if scene_number > 0 else fallback


def _build_scene_prompt_payload(
    *,
    act_number: int,
    act: dict,
    scene: dict,
    scene_number: int,
    scene_claims: list[dict],
    scene_evidence: list[dict],
    hosts: list[dict],
    forbidden_phrases: list[str],
    max_reply_tokens: int,
    language: str,
    previous_scene: dict | None,
    previous_act_summary: dict | None,
    already_covered_claims: list[dict],
    already_covered_claims_global: list[dict],
    recent_scene_starts: list[str],
    new_information_goal: str,
) -> dict:
    return {
        "language": str(language or "en").strip() or "en",
        "act_number": act_number,
        "act_title": act.get("title", ""),
        "scene_number": scene_number,
        "scene_type": scene.get("type", "explanation"),
        "scene_claim_mode": _scene_claim_mode(scene),
        "scene_job": _scene_job(scene),
        "callback_claim_ids": _scene_callback_claim_ids(scene),
        "description": scene.get("description", ""),
        "transition_note": scene.get("transition_note", ""),
        "notes": scene.get("notes", ""),
        "estimated_duration_seconds": scene.get("estimated_duration_seconds"),
        "previous_scene": previous_scene,
        "previous_act_summary": previous_act_summary,
        "already_covered_claims": already_covered_claims,
        "already_covered_claims_global": already_covered_claims_global,
        "recent_scene_starts": recent_scene_starts,
        "new_information_goal": new_information_goal,
        "claims": scene_claims,
        "evidence": scene_evidence,
        "hosts": hosts,
        "forbidden_phrases": forbidden_phrases,
        "max_reply_tokens": max_reply_tokens,
    }


async def _load_claim_prompt_payloads(db, claim_ids: list[str]) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    claim_uuid_map: dict[uuid.UUID, str] = {}
    for cid in claim_ids:
        try:
            claim_uuid = uuid.UUID(cid)
        except (TypeError, ValueError):
            continue
        claim_uuid_map[claim_uuid] = cid

    if not claim_uuid_map:
        return payloads

    result = await db.execute(select(Claim).where(Claim.id.in_(list(claim_uuid_map.keys()))))
    for claim in result.scalars().all():
        original_id = claim_uuid_map.get(claim.id)
        if original_id is None:
            continue
        payloads[original_id] = {
            "id": str(claim.id),
            "type": claim.claim_type,
            "verification": claim.verification_level,
            "statement": claim.statement[:300],
            "attribution": claim.attribution,
        }
    return payloads


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


def _scene_manifest_claim_ids(scene: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for source in (
        _uuid_list(scene.get("primary_claim_ids")),
        _uuid_list(scene.get("callback_claim_ids")),
        _uuid_list(scene.get("claim_ids")),
    ):
        for claim_id in source:
            if claim_id in seen:
                continue
            seen.add(claim_id)
            ordered.append(claim_id)
    return ordered


def _build_previous_scene_payload(
    beat_sheet: list[dict],
    current_scene_idx: int,
    *,
    act_number: int,
    existing_segments: dict[tuple[int, int], ScriptSegment],
    claim_lookup: dict[str, dict],
) -> dict | None:
    if current_scene_idx <= 1:
        return None
    previous = beat_sheet[current_scene_idx - 2]
    if not isinstance(previous, dict):
        return None

    previous_scene_number = _normalized_scene_number(previous.get("scene"), current_scene_idx - 1)
    previous_segment = existing_segments.get((act_number, previous_scene_number))
    previous_claim_ids = (
        _extract_dialogue_claim_ids(previous_segment.dialogue_json)
        if previous_segment is not None
        else _scene_manifest_claim_ids(previous)
    )
    established_points = _build_already_covered_claims_payload(
        covered_claim_ids=previous_claim_ids,
        claim_lookup=claim_lookup,
        max_items=4,
        statement_chars=PREVIOUS_SCENE_STATEMENT_CHARS,
    )
    established_claim_ids = [
        item.get("id")
        for item in established_points
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    what_listener_now_knows = "; ".join(
        item.get("statement", "")
        for item in established_points
        if isinstance(item.get("statement"), str) and item.get("statement", "").strip()
    ).strip()
    if not what_listener_now_knows:
        description = previous.get("description", "")
        if isinstance(description, str):
            what_listener_now_knows = _truncate_text_word_boundary(
                " ".join(description.strip().split()),
                220,
            )

    if previous_segment is not None:
        anchor_texts = _extract_dialogue_texts(previous_segment.dialogue_json)
    else:
        anchor_texts = [
            item.get("statement", "")
            for item in established_points
            if isinstance(item.get("statement"), str)
        ]
    last_line = (
        _extract_last_dialogue_line(previous_segment.dialogue_json)
        if previous_segment is not None
        else str(previous.get("transition_note", "") or "").strip()
    )

    return {
        "scene_number": previous_scene_number,
        "scene_type": previous.get("type", "explanation"),
        "what_listener_now_knows": what_listener_now_knows,
        "claim_ids": established_claim_ids or previous_claim_ids[:4],
        "mechanism_anchors": _collect_mechanism_anchors(anchor_texts),
        "last_line": last_line,
    }


def _build_previous_act_summary(
    acts_json: list[dict],
    current_act_idx: int,
    *,
    existing_segments: dict[tuple[int, int], ScriptSegment],
    claim_lookup: dict[str, dict],
) -> dict | None:
    if current_act_idx <= 1:
        return None
    previous_act = acts_json[current_act_idx - 2]
    if not isinstance(previous_act, dict):
        return None
    previous_act_number = int(previous_act.get("act_number") or (current_act_idx - 1))
    beat_sheet = previous_act.get("beat_sheet", [])
    last_scene = beat_sheet[-1] if isinstance(beat_sheet, list) and beat_sheet else {}
    if not isinstance(last_scene, dict):
        last_scene = {}
    previous_scene_numbers: list[int] = []
    if isinstance(beat_sheet, list):
        for scene_idx, scene in enumerate(beat_sheet, start=1):
            if not isinstance(scene, dict):
                continue
            previous_scene_numbers.append(_normalized_scene_number(scene.get("scene"), scene_idx))
    previous_dialogue_claim_ids: list[str] = []
    previous_dialogue_claim_ids_set: set[str] = set()
    last_written_segment: ScriptSegment | None = None
    for scene_number in previous_scene_numbers:
        segment = existing_segments.get((previous_act_number, scene_number))
        if segment is None:
            continue
        last_written_segment = segment
        _extend_unique_ids(
            ordered=previous_dialogue_claim_ids,
            seen=previous_dialogue_claim_ids_set,
            new_values=_extract_dialogue_claim_ids(segment.dialogue_json),
        )
    ending_turn = previous_act.get("ending_turn")
    if not isinstance(ending_turn, str):
        ending_turn = ""
    ending_turn = " ".join(ending_turn.strip().split())
    actual_summary_claims = _build_already_covered_claims_payload(
        covered_claim_ids=previous_dialogue_claim_ids,
        claim_lookup=claim_lookup,
        max_items=3,
        statement_chars=160,
    )
    if actual_summary_claims:
        what_listener_now_knows = " ".join(
            item.get("statement", "")
            for item in actual_summary_claims
            if isinstance(item.get("statement"), str) and item.get("statement", "").strip()
        ).strip()
    else:
        what_listener_now_knows = ending_turn or last_scene.get("description", "")
    last_line = _extract_last_dialogue_line(
        last_written_segment.dialogue_json if last_written_segment is not None else None
    )
    return {
        "act_number": previous_act_number,
        "act_title": previous_act.get("title", ""),
        "ending_scene_type": last_scene.get("type", "takeaway"),
        "what_listener_now_knows": what_listener_now_knows,
        "bridge_to_current_act": last_line or last_scene.get("transition_note", ""),
    }


def _collect_manifest_claim_ids(acts_json: object) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    if not isinstance(acts_json, list):
        return ordered
    for act in acts_json:
        if not isinstance(act, dict):
            continue
        beat_sheet = act.get("beat_sheet", [])
        if not isinstance(beat_sheet, list):
            continue
        for scene in beat_sheet:
            if not isinstance(scene, dict):
                continue
            for claim_id in _scene_manifest_claim_ids(scene):
                if not isinstance(claim_id, str) or not claim_id or claim_id in seen:
                    continue
                seen.add(claim_id)
                ordered.append(claim_id)
    return ordered


def _build_new_information_goal(
    *,
    scene: dict,
    scene_claims: list[dict],
    local_covered_claim_ids: set[str],
    global_covered_claim_ids: set[str],
) -> str:
    new_statements: list[str] = []
    seen_statements: set[str] = set()
    for claim in scene_claims:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("id")
        if (
            isinstance(claim_id, str)
            and (claim_id in local_covered_claim_ids or claim_id in global_covered_claim_ids)
        ):
            continue
        statement = claim.get("statement")
        if not isinstance(statement, str):
            continue
        cleaned = " ".join(statement.strip().split())
        if not cleaned or cleaned in seen_statements:
            continue
        seen_statements.add(cleaned)
        new_statements.append(cleaned[:220])
        if len(new_statements) >= 2:
            break

    if new_statements:
        return "Advance the listener with what is new here: " + "; ".join(new_statements)

    description = scene.get("description", "")
    if isinstance(description, str):
        cleaned_description = " ".join(description.strip().split())
        if cleaned_description:
            return (
                "Do not reteach already covered setup. Use this scene to add a fresh angle, "
                f"consequence, or reframing from the brief: {cleaned_description[:260]}"
            )

    return "Do not reteach already covered material. Add only the fresh angle, consequence, or reframing this scene contributes."


def _build_recent_scene_starts(
    beat_sheet: list[dict],
    current_scene_idx: int,
    *,
    act_number: int,
    existing_segments: dict[tuple[int, int], ScriptSegment],
) -> list[str]:
    if current_scene_idx <= 1:
        return []

    recent_scene_starts: list[str] = []
    start_index = max(1, current_scene_idx - RECENT_SCENE_STARTS_MAX_ITEMS)
    for scene_idx in range(start_index, current_scene_idx):
        scene = beat_sheet[scene_idx - 1]
        if not isinstance(scene, dict):
            continue
        scene_number = _normalized_scene_number(scene.get("scene"), scene_idx)
        segment = existing_segments.get((act_number, scene_number))
        if segment is None:
            continue
        opener = _extract_dialogue_opener(segment.dialogue_json)
        if opener:
            recent_scene_starts.append(opener)
    return recent_scene_starts[-RECENT_SCENE_STARTS_MAX_ITEMS:]


def _build_already_covered_claims_payload(
    *,
    covered_claim_ids: list[str],
    claim_lookup: dict[str, dict],
    max_items: int | None = None,
    statement_chars: int = 300,
    include_mechanism_anchors: bool = False,
) -> list[dict]:
    covered: list[dict] = []
    relevant_claim_ids = covered_claim_ids[-max_items:] if isinstance(max_items, int) and max_items > 0 else covered_claim_ids
    statement_limit = statement_chars if isinstance(statement_chars, int) and statement_chars > 0 else 300
    for cid in relevant_claim_ids:
        claim_payload = claim_lookup.get(cid)
        if claim_payload is None:
            continue
        statement = claim_payload.get("statement", "")
        if not isinstance(statement, str):
            statement = ""
        payload = {
            "id": cid,
            "statement": statement[:statement_limit],
        }
        if include_mechanism_anchors:
            payload["mechanism_anchors"] = _extract_mechanism_anchors(statement)
        covered.append(payload)
    return covered


def _truncate_text_word_boundary(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    truncated = cleaned[:limit]
    if " " not in truncated:
        return truncated
    return truncated.rsplit(" ", 1)[0]


def _extract_dialogue_texts(dialogue_json: object) -> list[str]:
    ordered: list[str] = []
    if not isinstance(dialogue_json, list):
        return ordered
    for line in dialogue_json:
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if not isinstance(text, str):
            continue
        cleaned = " ".join(text.strip().split())
        if cleaned:
            ordered.append(cleaned)
    return ordered


def _extract_dialogue_opener(dialogue_json: object, *, max_words: int = 2) -> str:
    texts = _extract_dialogue_texts(dialogue_json)
    if not texts:
        return ""
    first_text = texts[0]
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)?", first_text, flags=re.UNICODE)
    if not words:
        return ""
    return " ".join(word.lower() for word in words[:max_words])


def _collect_mechanism_anchors(texts: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        for anchor in _extract_mechanism_anchors(text):
            if anchor in seen:
                continue
            seen.add(anchor)
            ordered.append(anchor)
    return ordered[:6]


def _extract_mechanism_anchors(statement: str) -> list[str]:
    if not isinstance(statement, str) or not statement.strip():
        return []

    anchors: list[str] = []
    seen: set[str] = set()

    numeric_patterns = (
        r"(?:[A-Z]{1,4}\$|[$€£¥₽])\s*\d+(?:[.,]\d+)?(?:\s*(?:bn|mn|m|b|million|billion|млн|млрд|трлн))?",
        r"\b\d+(?:[.,]\d+)?\s*%",
        r"(?<![$€£¥₽])\b\d+[.,]\d+\b",
    )
    for pattern in numeric_patterns:
        for match in re.findall(pattern, statement):
            cleaned = " ".join(match.strip().split())
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                anchors.append(cleaned)

    for match in re.findall(r"\b[A-Z]{3,}(?:\s+[A-Z]{2,})*\b(?!\$)", statement):
        cleaned = " ".join(match.strip().split())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            anchors.append(cleaned)

    title_patterns = (
        r"\b[A-Z][a-z]+(?:\s+(?:of|the|and|for|to|in|on|de|del|da|di|du|la|le))?\s+[A-Z][a-z]+(?:\s+(?:(?:of|the|and|for|to|in|on|de|del|da|di|du|la|le)|[A-Z][a-z]+)){0,4}\b",
        r"\b[А-ЯЁ][а-яё]+(?:\s+(?:и|по|на|о|в|во|при|под|над|с|со|из|от|для|без))?\s+[А-ЯЁ][а-яё]+(?:\s+(?:(?:и|по|на|о|в|во|при|под|над|с|со|из|от|для|без)|[А-ЯЁ][а-яё]+)){0,4}\b",
    )
    for pattern in title_patterns:
        for match in re.findall(pattern, statement):
            cleaned = match.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                anchors.append(cleaned)

    return anchors[:6]


def _extract_dialogue_claim_ids(dialogue_json: object) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    if not isinstance(dialogue_json, list):
        return ordered
    for line in dialogue_json:
        if not isinstance(line, dict):
            continue
        claim_ids = line.get("claim_ids")
        if not isinstance(claim_ids, list):
            continue
        for claim_id in claim_ids:
            if not isinstance(claim_id, str) or not claim_id or claim_id in seen:
                continue
            seen.add(claim_id)
            ordered.append(claim_id)
    return ordered


def _extract_last_dialogue_line(dialogue_json: object) -> str:
    if not isinstance(dialogue_json, list):
        return ""
    for line in reversed(dialogue_json):
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _dump_screenwriter_debug(
    *,
    run_id: str,
    act_number: int,
    scene_number: int,
    status: str,
    prompt_payload: dict,
    attempts: list[dict[str, Any]],
    dialogue: list[dict] | None,
    error: str | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return

    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="screenwriter",
        filename_stem=(
            f"run-{safe_filename_token(run_id)}_"
            f"act-{act_number:02d}_scene-{scene_number:02d}_{safe_filename_token(status)}"
        ),
        payload={
            "run_id": run_id,
            "act_number": act_number,
            "scene_number": scene_number,
            "status": status,
            "input": prompt_payload,
            "attempts": attempts,
            "output": {
                "dialogue_line_count": len(dialogue or []),
                "dialogue": dialogue,
            },
            "error": error,
        },
    )
