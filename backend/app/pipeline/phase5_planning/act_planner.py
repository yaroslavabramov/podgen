"""Phase 5.2: Act Planner — beat sheet per act."""

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
from app.models.source import Source
from app.services.llm import call_sonnet_structured, call_haiku_structured
from app.services.evidence_store import (
    EvidenceProvenanceContext,
    ensure_evidence_provenance,
    get_or_create_canonical_evidence,
)
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.phase3_research.tools import vector_search_tool
from app.realtime.publisher import publish_event
from app.realtime.events import act_planned, step_started, step_completed

logger = logging.getLogger(__name__)
ACT_PLANNER_RETRIES = 2
MAX_EVIDENCE_SEARCHES_PER_ACT = 3
MAX_SUPPORT_EVIDENCE_PER_ACT = 30
MAX_DIRECT_SUPPORT_PER_CLAIM = 1
MAX_ILLUSTRATIVE_SUPPORT_PER_CLAIM = 1
MAX_EXISTING_EVIDENCE_IDS_PER_SCENE = 2

EVIDENCE_EXTRACT_SYSTEM = """You are an evidence extractor. Given a chunk of text and a scene description, extract a relevant verbatim quote that could serve as evidence for the scene.

Return JSON:
{
  "quote": "exact verbatim text from the chunk",
  "asset_type": "anecdote|case|contrast|statistic|expert_quote|definition|mechanism|timeline",
  "relevance_score": 0.8,
  "direct_or_contextual": "direct|contextual"
}

If the chunk has no relevant evidence for this scene, return:
{"quote": null, "asset_type": null, "relevance_score": 0.0}"""

ACT_PLANNER_SYSTEM = """You are a podcast act planner creating a beat sheet (scene list).
Given an act's role, purpose, claims with support bundles, and quotas, create a detailed beat sheet that feels like story movement, not a flat list of explanations.

Return JSON:
{
  "scene_strategy": "short phrase describing the act's internal movement",
  "beat_sheet": [
    {
      "scene": 1,
      "type": "hook|explanation|debate|case_study|transition|summary|takeaway",
      "description": "1-3 sentence beat card describing what changes in this scene",
      "claim_ids": ["id"],
      "evidence_ids": ["id"],
      "estimated_duration_seconds": 45,
      "transition_note": "short bridge to the next scene or null",
      "notes": "short host-dynamic or production cue, not script prose"
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
- takeaway: actionable insight

Use claim/evidence metadata:
- `has_illustrative` and `illustrative_evidence_count` indicate narrative hooks
- `direct_support_count` and `source_diversity` indicate factual backbone
- each claim includes `support_bundle.direct` and optional `support_bundle.illustrative`
- bundled illustrative evidence is good for hooks, case-study beats, and vivid transitions

Rules:
- Language:
  - All human-readable output fields (`scene_strategy`, `description`, `transition_note`, `notes`) MUST be in `output_language` from the user payload.
  - Keep the output in the same language as the act contract unless `output_language` says otherwise.
  - Do not mix languages inside one field.
- The act must feel like it goes somewhere. Scenes should create setup -> development -> turn -> landing, not just restate adjacent facts.
- Before drafting scenes, choose a short `scene_strategy` that captures how this act moves internally.
- Respect the act contract:
  - `story_role` says what kind of movement this act should deliver
  - `purpose` says why the act exists
  - `internal_arc` says how the act should move internally
  - the first scene must explicitly open the act's `opening_question`
  - the final scene must clearly deliver the act's `ending_turn`
- Opening the act question does not require a `hook`. The first scene may be `case_study`, `explanation`, or `transition` when that creates a stronger entrance for this act.
- Landing the act turn does not require a `takeaway`. The final scene may land through `case_study`, `summary`, `explanation`, or `transition` when that better fits the act.
- Do not default every act to `hook` at the start and `takeaway` at the end.
- Avoid the default podcast skeleton of hook -> explanation -> case_study -> takeaway unless the act genuinely needs it. Reusing the same internal shape across acts makes the episode feel mechanically repetitive.
- Act-to-act variety matters. Do not default every act to the same internal shape.
- Within an act, adjacent scenes should usually do different work: open a question, sharpen it, ground it in an example, reveal a mechanism, stress-test it, or land a consequence.
- If two neighboring scenes mainly explain the same thing in the same mode, merge them or change the angle.
- Each act should contain at least one meaningful turn: a realization, complication, stress test, reversal, or reframing that changes what the listener now understands.
- Do not turn the act into a laundry list of explanations.
- Keep one clear job per scene. Split only when the listener would feel a meaningful shift.
- Avoid repeating the same proposition core across multiple scenes in the same act.
- Use `scene_count_guidance` as the default size target for the act. Do not exceed it unless the act would otherwise feel structurally incomplete.
- Prefer compact beat cards, not mini-script paragraphs. `description` should describe the beat, not write the scene. `notes` should stay short.
- When a scene uses a claim, attach one or more `evidence_ids` from that claim's bundled support whenever available.
- Prefer bundled claim support over improvising unsupported scenes or assuming an evidence search will rescue the beat later.
- Alternate dense explanation with a vivid example, hook, case, contrast, or transition whenever the evidence allows it.
- A `case_study` scene is optional, not mandatory. Use it only when a concrete episode materially advances the act.
- If the act has illustrative material, use it early or at key pivots instead of burying it at the end.
- Do not stack more than two `explanation` scenes in a row without a meaningful shift in angle, example, or tension.
- Use `debate` only when the act has real conflict pressure (`conflict_count > 0`) or the act itself is genuinely comparative/argumentative. If there is no real conflict, do not invent one.
- Let `story_role` shape the act:
  - `setup`: establish the paradox, world, and stakes
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
            act_claims = [claim_contexts[cid] for cid in raw_claim_ids if cid in claim_contexts]
            act_evidence = list(evidence_contexts.values())

            allowed_claim_ids = {c["id"] for c in act_claims}
            allowed_evidence_ids = {e["id"] for e in act_evidence}
            per_scene_duration = max(
                30,
                _positive_int(act.get("estimated_duration_seconds"), 300) // 6,
            )

            base_prompt = _build_act_prompt_payload(
                act=act,
                act_number=act_num,
                act_claims=act_claims,
                act_evidence=act_evidence,
                output_language=state.get("language", "en"),
            )
            claim_support_lookup = _claim_support_lookup(base_prompt["claims"])

            beat_sheet: list[dict[str, Any]] = []
            scene_strategy = ""
            last_error = ""
            attempt_debugs: list[dict[str, Any]] = []
            for attempt in range(1, ACT_PLANNER_RETRIES + 1):
                prompt_payload = dict(base_prompt)
                if attempt > 1:
                    prompt_payload["strict_output_contract"] = (
                        "Return valid JSON with top-level keys scene_strategy and beat_sheet. No prose."
                    )
                    prompt_payload["previous_error"] = last_error

                prompt = json.dumps(prompt_payload, ensure_ascii=False)
                raw = ""
                beat_data = None
                try:
                    raw = await call_sonnet_structured(system=ACT_PLANNER_SYSTEM, prompt=prompt, temperature=0.5)
                    beat_data = parse_llm_json(raw)
                    scene_strategy = _extract_scene_strategy(beat_data)
                    beat_sheet = _normalize_beat_sheet(
                        beat_data,
                        allowed_claim_ids=allowed_claim_ids,
                        allowed_evidence_ids=allowed_evidence_ids,
                        default_duration=per_scene_duration,
                    )
                    if beat_sheet:
                        attempt_debugs.append(
                            {
                                "attempt": attempt,
                                "status": "ok",
                                "prompt_payload": prompt_payload,
                                "llm_output": {
                                    "raw_chars": len(raw or ""),
                                    "raw": raw,
                                    "parsed_type": type(beat_data).__name__ if beat_data is not None else None,
                                    "parsed": beat_data,
                                },
                                "normalized_scene_count": len(beat_sheet),
                            }
                        )
                        break
                    raise ValueError("beat_sheet is empty after normalization")
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    attempt_debugs.append(
                        {
                            "attempt": attempt,
                            "status": "invalid_output",
                            "prompt_payload": prompt_payload,
                            "llm_output": {
                                "raw_chars": len(raw or ""),
                                "raw": raw,
                                "parsed_type": type(beat_data).__name__ if beat_data is not None else None,
                                "parsed": beat_data,
                            },
                            "error": last_error,
                        }
                    )
                    logger.warning(
                        "Act planning output invalid for act %s (attempt %s/%s): %s",
                        act_num,
                        attempt,
                        ACT_PLANNER_RETRIES,
                        last_error,
                        exc_info=True,
                    )

            if not beat_sheet:
                _dump_act_planner_debug(
                    run_id=run_id,
                    act_number=act_num,
                    status="llm_or_normalize_failed",
                    base_prompt=base_prompt,
                    attempts=attempt_debugs,
                    beat_sheet=None,
                    gap_fill_result=None,
                )
                raise RuntimeError(
                    f"Act planner produced empty beat sheet for act {act_num}. Last error: {last_error}"
                )

            if scene_strategy:
                act["scene_strategy"] = scene_strategy
            else:
                act.pop("scene_strategy", None)

            hydrated_existing_evidence_ids = _hydrate_scene_evidence_ids_from_claim_support(
                beat_sheet,
                claim_support_lookup,
            )

            # Fill evidence gaps from existing corpus
            try:
                new_ev_ids = await _fill_evidence_gaps(
                    act,
                    beat_sheet,
                    db,
                    state["project_id"],
                    generation_run_id=state["generation_run_id"],
                    manifest_id=state["manifest_id"],
                )
                if new_ev_ids:
                    logger.info("Act %s: filled %d evidence gaps", act_num, len(new_ev_ids))
            except Exception:
                logger.warning("Evidence gap fill failed for act %s, continuing", act_num, exc_info=True)
                new_ev_ids = []

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
                    "new_evidence_ids": new_ev_ids,
                    "new_evidence_count": len(new_ev_ids),
                },
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


NARRATIVE_SCENE_TYPES = {"case_study", "debate", "hook"}


async def _fill_evidence_gaps(
    act: dict, beat_sheet: list[dict], db, project_id: str,
    *,
    generation_run_id: str | None = None,
    manifest_id: str | None = None,
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

            evidence, _ = await get_or_create_canonical_evidence(
                db=db,
                project_id=project_id,
                source_id=best_chunk["source_id"],
                verbatim_quote=extracted["quote"][:2000],
                asset_type=extracted.get("asset_type", "case"),
                narrative_value_score=extracted.get("relevance_score", 0.5),
            )
            if generation_run_id and manifest_id:
                act_number = act.get("act_number") or act.get("act") or 0
                await ensure_evidence_provenance(
                    db=db,
                    evidence_id=evidence.id,
                    context=EvidenceProvenanceContext(
                        generation_run_id=generation_run_id,
                        manifest_id=manifest_id,
                        question_id=f"act:{act_number}:scene:{scene.get('scene')}",
                        branch_id=f"act:{act_number}",
                        asset_type=extracted.get("asset_type", "case"),
                        retrieved_by="act_planner",
                        query_text=query,
                        retrieval_score=float(extracted.get("relevance_score", 0.5)),
                        direct_or_contextual=extracted.get("direct_or_contextual"),
                    ),
                )

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


def _extract_scene_strategy(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get("scene_strategy")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _branch_focus_summary(act_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    branch_counts: dict[str, int] = {}
    for claim in act_claims:
        for branch_id in claim.get("branch_ids", []) or []:
            branch_counts[branch_id] = branch_counts.get(branch_id, 0) + 1
    return [
        {"branch_id": branch_id, "claim_count": count}
        for branch_id, count in sorted(branch_counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _build_act_prompt_payload(
    *,
    act: dict[str, Any],
    act_number: int,
    act_claims: list[dict[str, Any]],
    act_evidence: list[dict[str, Any]],
    output_language: str,
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
        "focus_branch_ids": [
            branch_id
            for branch_id in (act.get("focus_branch_ids") or [])
            if isinstance(branch_id, str) and branch_id.strip()
        ],
        "branch_focus_summary": _branch_focus_summary(act_claims),
        "estimated_duration_seconds": estimated_duration,
        "scene_count_guidance": _scene_count_guidance(estimated_duration),
        "content_quotas": act.get("content_quotas", {}) if isinstance(act.get("content_quotas"), dict) else {},
        "conflict_count": len(conflict_ids),
        "claims": prompt_claims,
    }


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
                "branch_ids": [
                    branch_id
                    for branch_id in (claim.get("branch_ids") or [])
                    if isinstance(branch_id, str) and branch_id.strip()
                ],
                "intents": [
                    intent
                    for intent in (claim.get("intents") or [])
                    if isinstance(intent, str) and intent.strip()
                ],
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
        claim_ids = _uuid_list(scene.get("claim_ids"))
        scene["evidence_ids"] = existing_ids
        if not claim_ids:
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

        for mode in preferred_modes:
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


def _dump_act_planner_debug(
    *,
    run_id: str,
    act_number: int,
    status: str,
    base_prompt: dict,
    attempts: list[dict[str, Any]],
    beat_sheet: list[dict[str, Any]] | None,
    gap_fill_result: dict[str, Any] | None,
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
        },
    )


def _next_available_scene_number(candidate: int, used: set[int]) -> int:
    scene_number = candidate if candidate > 0 else 1
    while scene_number in used:
        scene_number += 1
    return scene_number
