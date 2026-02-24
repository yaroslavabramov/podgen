"""Phase 6.1: Screenwriter — generate dialogue per scene."""

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
from app.models.script_segment import ScriptSegment
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, scene_written, progress

logger = logging.getLogger(__name__)

SCREENWRITER_SYSTEM = """You are a podcast screenwriter creating natural dialogue between two hosts.
Given a scene description, claims, evidence, and host personas, write the dialogue.

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
- VERIFIED → speak confidently ("Studies confirm...", "It's established that...")
- OBSERVED → hedge ("According to reports...", "Data suggests...")
- UNVERIFIED → strong hedge ("Some claim...", "There are unconfirmed reports...")
- DISPUTED → present both sides ("Interestingly, there's debate here...")
- OPINION → attribute ("Karpathy believes...", "As X puts it...")
- REPORT → recount ("The report states...", "According to the investigation...")

Host A = Guide: leads narrative, explains, summarizes
Host B = Challenger: questions, demands proof, plays devil's advocate

Keep replies under {max_reply_tokens} tokens. Natural, conversational tone.
NEVER use any forbidden phrases."""


async def write_screenplay(state: PipelineState) -> dict:
    """LangGraph node: write dialogue for all acts sequentially."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase6_writing", "Writing podcast screenplay"))
    update_generation_progress(run_id, "phase6_writing", "screenwriter", 62.0)

    all_segment_ids = []
    planned_scene_count = 0
    total_dialogue_lines = 0

    async with async_session_factory() as db:
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

        max_tokens = format_rules.get("max_reply_tokens", 120)

        for act in manifest.acts_json:
            act_num = act.get("act_number", 0)
            beat_sheet = act.get("beat_sheet", [])
            if not isinstance(beat_sheet, list):
                logger.warning("Act %s beat_sheet has invalid type: %s", act_num, type(beat_sheet).__name__)
                continue

            for scene in beat_sheet:
                if not isinstance(scene, dict):
                    continue
                scene_num = scene.get("scene", 0)
                planned_scene_count += 1
                publish_event(run_id, step_started("phase6_writing", f"write_{act_num}_{scene_num}", f"Writing Act {act_num}, Scene {scene_num}"))

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

                prompt = json.dumps({
                    "act_number": act_num,
                    "scene_number": scene_num,
                    "scene_type": scene.get("type", "explanation"),
                    "description": scene.get("description", ""),
                    "notes": scene.get("notes", ""),
                    "claims": scene_claims,
                    "evidence": scene_evidence,
                    "hosts": hosts,
                    "forbidden_phrases": forbidden,
                    "max_reply_tokens": max_tokens,
                    "language": state["language"],
                })

                try:
                    raw = await call_sonnet_structured(
                        system=SCREENWRITER_SYSTEM.format(max_reply_tokens=max_tokens),
                        prompt=prompt,
                        max_tokens=4096,
                        temperature=0.7,
                    )
                    parsed = parse_llm_json(raw)
                    if not isinstance(parsed, list):
                        raise ValueError(f"screenwriter output must be a JSON array, got {type(parsed).__name__}")

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
                except Exception:
                    logger.error("Screenwriting failed for act %d scene %d", act_num, scene_num, exc_info=True)
                    dialogue = []

                if not dialogue:
                    logger.warning("Skipping empty dialogue for act %s scene %s", act_num, scene_num)
                    publish_event(run_id, step_completed("phase6_writing", f"write_{act_num}_{scene_num}"))
                    continue

                segment = ScriptSegment(
                    id=uuid.uuid4(),
                    generation_run_id=uuid.UUID(run_id),
                    act_number=act_num,
                    scene_number=scene_num,
                    dialogue_json=dialogue,
                )
                db.add(segment)
                await db.flush()
                all_segment_ids.append(str(segment.id))
                total_dialogue_lines += len(dialogue)

                publish_event(run_id, scene_written(act_num, scene_num))
                publish_event(run_id, step_completed("phase6_writing", f"write_{act_num}_{scene_num}"))

        if planned_scene_count == 0:
            raise RuntimeError("Screenwriter received an empty beat sheet; no scenes to write")
        if total_dialogue_lines == 0:
            raise RuntimeError("Screenwriter produced no dialogue lines")

        await db.commit()

    publish_event(run_id, progress(75.0, f"Wrote {len(all_segment_ids)} scenes"))
    update_generation_progress(run_id, "phase6_writing", "screenwriter_done", 75.0)

    return {"script_segment_ids": all_segment_ids, "current_phase": "phase6_writing"}
