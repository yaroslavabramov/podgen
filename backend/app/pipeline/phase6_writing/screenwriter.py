"""Phase 6.1: Screenwriter — generate dialogue per scene."""

import asyncio
import json
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.show_bible import ShowBible
from app.models.claim import Claim
from app.models.evidence import EvidenceSpan
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.llm_json import parse_llm_json
from app.models.script_segment import ScriptSegment, AuditStatus
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, scene_written, progress

logger = logging.getLogger(__name__)
SCENE_GENERATION_ATTEMPTS = 3
SCENE_RETRY_BASE_DELAY_SECONDS = 1.0

SCREENWRITER_SYSTEM = """You are a podcast screenwriter creating natural dialogue between two hosts.
Given an act contract, scene brief, claims, evidence, and host personas, write the dialogue.

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

Host A = Guide: leads narrative, explains, summarizes
Host B = Challenger: questions, demands proof, plays devil's advocate

Writing rules:
- Language:
  - All dialogue lines MUST be in the `language` from the user payload.
  - Do not mix languages inside one line unless the evidence itself requires a short quoted term.
- Use the act contract (`story_role`, `act_purpose`, `internal_arc`, `scene_strategy`) to make the scene feel like part of a larger arc, not an isolated explainer.
- Use `description` and `transition_note` as scene goals, not text to paraphrase verbatim.
- Keep the scene moving. Do not let both hosts restate the same point in slightly different words.
- Avoid dry explanatory dialogue by default. Prefer a concrete opening move, a turn, and a landing.
- Let `scene_type` shape the scene:
  - `hook`: open with surprise, tension, or a vivid listener-facing question
  - `case_study`: ground the scene in a concrete event, example, or episode
  - `explanation`: clarify only the mechanism needed for this beat; do not drift into lecture mode
  - `debate`: surface real tension or tradeoff, not a fake argument
  - `transition`: pivot the listener cleanly from one framing to another
  - `summary`: compress what changed in understanding without sounding like bullet points
  - `takeaway`: land the consequence or implication, not just a generic recap
- Do not make Host A always explain and Host B always ask for proof in the same rhythm. Vary who initiates and who reframes.
- Use evidence organically. Do not dump raw quotes or source language into dialogue unless quoting is the point of the beat.
- When claims are OBSERVED, UNVERIFIED, DISPUTED, OPINION, or REPORT, make the line sound appropriately attributed or qualified.

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

        for act in manifest.acts_json:
            act_num = act.get("act_number", 0)
            beat_sheet = act.get("beat_sheet", [])
            if not isinstance(beat_sheet, list):
                logger.warning("Act %s beat_sheet has invalid type: %s", act_num, type(beat_sheet).__name__)
                continue

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
                    and existing_segment.audit_status == AuditStatus.REVISED
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
                    publish_event(run_id, scene_written(act_num, scene_num))
                    publish_event(run_id, step_completed("phase6_writing", f"write_{act_num}_{scene_num}"))
                    continue

                # Gather claims and evidence for this scene
                scene_claims = []
                for cid in scene.get("claim_ids", []):
                    r = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
                    c = r.scalar_one_or_none()
                    if c:
                        scene_claims.append({
                            "id": str(c.id), "type": c.claim_type,
                            "verification": c.verification_level, "statement": c.statement[:300],
                            "attribution": c.attribution,
                        })

                scene_evidence = []
                for eid in scene.get("evidence_ids", []):
                    r = await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == uuid.UUID(eid)))
                    e = r.scalar_one_or_none()
                    if e:
                        scene_evidence.append({
                            "id": str(e.id), "quote": e.verbatim_quote[:300],
                            "asset_type": e.asset_type, "tellability": e.tellability_notes,
                        })

                prompt = json.dumps(
                    _build_scene_prompt_payload(
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
                    ),
                    ensure_ascii=False,
                )

                dialogue = []
                last_error: Exception | None = None
                for attempt in range(1, SCENE_GENERATION_ATTEMPTS + 1):
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
                            speaker = line.get("speaker")
                            line["speaker"] = speaker if isinstance(speaker, str) and speaker.strip() else "A"
                            line["text"] = text.strip()
                            if not isinstance(line.get("ssml_hints"), dict):
                                line["ssml_hints"] = {}
                            if not isinstance(line.get("claim_ids"), list):
                                line["claim_ids"] = []
                            dialogue.append(line)

                        if not dialogue:
                            raise ValueError("screenwriter output contains no valid dialogue lines")
                        break
                    except Exception as exc:
                        last_error = exc
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


def _append_unique_segment_id(segment_id: str, ordered: list[str], seen: set[str]) -> None:
    if segment_id in seen:
        return
    seen.add(segment_id)
    ordered.append(segment_id)


def _normalized_scene_number(value: object, fallback: int) -> int:
    try:
        scene_number = int(value)
    except (TypeError, ValueError):
        return fallback
    return scene_number if scene_number > 0 else fallback


def _build_scene_prompt_payload(
    *,
    act: dict,
    act_number: int,
    scene: dict,
    scene_number: int,
    scene_claims: list[dict],
    scene_evidence: list[dict],
    hosts: list[dict],
    forbidden_phrases: list[str],
    max_reply_tokens: int,
    language: str,
) -> dict:
    return {
        "language": str(language or "en").strip() or "en",
        "act_number": act_number,
        "act_title": act.get("title", ""),
        "story_role": act.get("story_role", ""),
        "act_purpose": act.get("purpose", ""),
        "internal_arc": act.get("internal_arc", ""),
        "opening_question": act.get("opening_question", ""),
        "ending_turn": act.get("ending_turn", ""),
        "scene_strategy": act.get("scene_strategy", ""),
        "scene_number": scene_number,
        "scene_type": scene.get("type", "explanation"),
        "description": scene.get("description", ""),
        "transition_note": scene.get("transition_note", ""),
        "notes": scene.get("notes", ""),
        "estimated_duration_seconds": scene.get("estimated_duration_seconds"),
        "claims": scene_claims,
        "evidence": scene_evidence,
        "hosts": hosts,
        "forbidden_phrases": forbidden_phrases,
        "max_reply_tokens": max_reply_tokens,
    }
