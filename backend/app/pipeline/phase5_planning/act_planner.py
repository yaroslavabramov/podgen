"""Phase 5.2: Act Planner — beat sheet per act."""

import copy
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.claim import Claim
from app.models.evidence import EvidenceSpan
from app.models.claim import ClaimEvidence
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.state import PipelineState
from app.models.source import Source
from app.services.llm import call_sonnet_structured, call_haiku_structured
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.phase3_research.tools import vector_search_tool
from app.realtime.publisher import publish_event
from app.realtime.events import act_planned, step_started, step_completed

logger = logging.getLogger(__name__)
ACT_PLANNER_RETRIES = 2
MAX_EVIDENCE_SEARCHES_PER_ACT = 3

EVIDENCE_EXTRACT_SYSTEM = """You are an evidence extractor. Given a chunk of text and a scene description, extract a relevant verbatim quote that could serve as evidence for the scene.

Return JSON:
{
  "quote": "exact verbatim text from the chunk",
  "asset_type": "anecdote|case|contrast|statistic|expert_quote|definition|mechanism|timeline",
  "relevance_score": 0.8
}

If the chunk has no relevant evidence for this scene, return:
{"quote": null, "asset_type": null, "relevance_score": 0.0}"""

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

        if not isinstance(manifest.acts_json, list):
            raise RuntimeError("Act planner expected acts_json list in manifest")

        # JSONB nested in-place mutation is not tracked reliably by SQLAlchemy.
        # Work on a detached copy and assign it back to guarantee persistence.
        acts = copy.deepcopy(manifest.acts_json)
        total_scene_count = 0

        for i, act in enumerate(acts, start=1):
            if not isinstance(act, dict):
                logger.warning("Skipping invalid act payload type: %s", type(act).__name__)
                continue

            act_num = _positive_int(act.get("act_number"), i)
            act["act_number"] = act_num
            publish_event(run_id, step_started("phase5_planning", f"plan_act_{act_num}", f"Planning Act {act_num}"))

            # Load claims for this act
            raw_claim_ids = _uuid_list(act.get("claim_ids"))
            act_claims = []
            for cid in raw_claim_ids:
                r = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
                c = r.scalar_one_or_none()
                if c:
                    act_claims.append({
                        "id": str(c.id), "type": c.claim_type,
                        "verification": c.verification_level, "statement": c.statement[:300],
                    })

            # Load evidence for act claims
            act_evidence = []
            for cid in raw_claim_ids:
                ce_result = await db.execute(select(ClaimEvidence).where(ClaimEvidence.claim_id == uuid.UUID(cid)))
                for ce in ce_result.scalars():
                    ev_result = await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == ce.evidence_id))
                    ev = ev_result.scalar_one_or_none()
                    if ev:
                        act_evidence.append({
                            "id": str(ev.id), "asset_type": ev.asset_type,
                            "quote": ev.verbatim_quote[:200], "narrative_score": ev.narrative_value_score,
                        })

            allowed_claim_ids = {c["id"] for c in act_claims}
            allowed_evidence_ids = {e["id"] for e in act_evidence}
            per_scene_duration = max(
                30,
                _positive_int(act.get("estimated_duration_seconds"), 300) // 6,
            )

            base_prompt = {
                "act_number": act_num,
                "act_title": act.get("title", ""),
                "estimated_duration_seconds": _positive_int(act.get("estimated_duration_seconds"), 300),
                "content_quotas": act.get("content_quotas", {}) if isinstance(act.get("content_quotas"), dict) else {},
                "claims": act_claims,
                "evidence": act_evidence[:20],
            }

            beat_sheet: list[dict[str, Any]] = []
            last_error = ""
            for attempt in range(1, ACT_PLANNER_RETRIES + 1):
                prompt_payload = dict(base_prompt)
                if attempt > 1:
                    prompt_payload["strict_output_contract"] = (
                        "Return valid JSON with top-level key beat_sheet. No prose."
                    )
                    prompt_payload["previous_error"] = last_error

                prompt = json.dumps(prompt_payload, ensure_ascii=False)
                try:
                    raw = await call_sonnet_structured(system=ACT_PLANNER_SYSTEM, prompt=prompt, temperature=0.5)
                    beat_data = parse_llm_json(raw)
                    beat_sheet = _normalize_beat_sheet(
                        beat_data,
                        allowed_claim_ids=allowed_claim_ids,
                        allowed_evidence_ids=allowed_evidence_ids,
                        default_duration=per_scene_duration,
                    )
                    if beat_sheet:
                        break
                    raise ValueError("beat_sheet is empty after normalization")
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Act planning output invalid for act %s (attempt %s/%s): %s",
                        act_num,
                        attempt,
                        ACT_PLANNER_RETRIES,
                        last_error,
                        exc_info=True,
                    )

            if not beat_sheet:
                raise RuntimeError(
                    f"Act planner produced empty beat sheet for act {act_num}. Last error: {last_error}"
                )

            # Fill evidence gaps from existing corpus
            try:
                new_ev_ids = await _fill_evidence_gaps(
                    act, beat_sheet, db, state["project_id"],
                )
                if new_ev_ids:
                    logger.info("Act %s: filled %d evidence gaps", act_num, len(new_ev_ids))
            except Exception:
                logger.warning("Evidence gap fill failed for act %s, continuing", act_num, exc_info=True)

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


NARRATIVE_SCENE_TYPES = {"case_study", "debate", "hook"}


async def _fill_evidence_gaps(
    act: dict, beat_sheet: list[dict], db, project_id: str,
) -> list[str]:
    """Detect evidence gaps in beat sheet and fill from existing corpus.

    Returns list of new evidence_ids added.
    """
    # Detect gaps: scenes of narrative types without evidence
    gap_scenes = []
    for scene in beat_sheet:
        scene_type = scene.get("type", "")
        if scene_type in NARRATIVE_SCENE_TYPES and not scene.get("evidence_ids"):
            gap_scenes.append(scene)

    # Also check: if entire act has < 1 narrative evidence, flag all scenes
    total_evidence = sum(len(s.get("evidence_ids", [])) for s in beat_sheet)
    if total_evidence == 0 and not gap_scenes:
        # Pick scenes that could benefit most from evidence
        gap_scenes = [s for s in beat_sheet if s.get("type") in ("explanation", "case_study", "hook")][:2]

    if not gap_scenes:
        return []

    # Load project source IDs for vector search
    source_result = await db.execute(
        select(Source.id).where(
            Source.project_id == uuid.UUID(project_id),
            Source.fully_ingested.is_(True),
        )
    )
    source_ids = [str(row[0]) for row in source_result.all()]
    if not source_ids:
        return []

    new_evidence_ids: list[str] = []
    searches_done = 0

    for scene in gap_scenes:
        if searches_done >= MAX_EVIDENCE_SEARCHES_PER_ACT:
            break

        query = scene.get("description", "")
        if not query:
            continue

        # Vector search in existing corpus
        chunks = await vector_search_tool(query, top_k=3, source_ids=source_ids, db=db)
        searches_done += 1

        if not chunks:
            continue

        # Use Haiku to extract evidence from top chunk
        best_chunk = chunks[0]
        try:
            prompt = json.dumps({
                "scene_description": query,
                "scene_type": scene.get("type", ""),
                "chunk_content": best_chunk["preview"],
                "source_id": best_chunk["source_id"],
            }, ensure_ascii=False)
            raw = await call_haiku_structured(
                system=EVIDENCE_EXTRACT_SYSTEM,
                prompt=prompt,
                max_tokens=1024,
                temperature=0.2,
            )
            extracted = parse_llm_json(raw)
            if not isinstance(extracted, dict) or not extracted.get("quote"):
                continue

            if extracted.get("relevance_score", 0) < 0.5:
                continue

            # Create EvidenceSpan
            evidence = EvidenceSpan(
                id=uuid.uuid4(),
                project_id=uuid.UUID(project_id),
                source_id=uuid.UUID(best_chunk["source_id"]),
                verbatim_quote=extracted["quote"][:2000],
                asset_type=extracted.get("asset_type", "case"),
                narrative_value_score=extracted.get("relevance_score", 0.5),
            )
            db.add(evidence)
            await db.flush()

            eid = str(evidence.id)
            scene.setdefault("evidence_ids", []).append(eid)
            new_evidence_ids.append(eid)
            logger.info(
                "Evidence gap filled: scene %s got evidence %s (type=%s)",
                scene.get("scene"), eid, extracted.get("asset_type"),
            )
        except Exception:
            logger.warning("Evidence extraction failed for scene %s", scene.get("scene"), exc_info=True)
            continue

    return new_evidence_ids


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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


def _extract_raw_scenes(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("beat_sheet", "scenes", "beats"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


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

        claim_ids = [
            cid for cid in _uuid_list(scene.get("claim_ids"))
            if not allowed_claim_ids or cid in allowed_claim_ids
        ]
        evidence_ids = [
            eid for eid in _uuid_list(scene.get("evidence_ids"))
            if not allowed_evidence_ids or eid in allowed_evidence_ids
        ]
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
                "description": description.strip(),
                "claim_ids": claim_ids,
                "evidence_ids": evidence_ids,
                "estimated_duration_seconds": duration,
                "transition_note": transition_note,
                "notes": notes.strip(),
            }
        )

    return normalized


def _next_available_scene_number(candidate: int, used: set[int]) -> int:
    scene_number = candidate if candidate > 0 else 1
    while scene_number in used:
        scene_number += 1
    return scene_number
