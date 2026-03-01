"""Phase 2: Query Decomposition — Research Tree + Duration Fill Strategy."""

import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.content_map import ContentMap
from app.models.episode_manifest import EpisodeManifest
from app.models.show_bible import ShowBible
from app.pipeline.state import PipelineState
from app.pipeline.phase2_decomposition.duration_strategy import compute_fill_layers
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, phase_completed, step_started, step_completed, progress

logger = logging.getLogger(__name__)

DECOMPOSER_SYSTEM = """You are a research decomposer for a long-form podcast.
Given topic, content_map, audience_profile, adjacency_policy, duration_target_seconds, and language, build a research tree.

Return valid JSON:
{
  "root_question": "string",
  "branches": [
    {
      "id": "b1",
      "question": "string",
      "intent": "history|explain|compare|debate|case_study",
      "expected_evidence_type": ["TIMELINE","CASE","EXPERT_QUOTE","STATISTIC","ANECDOTE","DEFINITION","MECHANISM","CONTRAST"],
      "quality_need": "VERIFIED|OBSERVED|ANY",
      "priority": 1,
      "explanation_hints": ["string"],
      "children": [
        {
          "id": "b1.1",
          "question": "string",
          "intent": "history|explain|compare|debate|case_study",
          "expected_evidence_type": ["TIMELINE","CASE","EXPERT_QUOTE","STATISTIC","ANECDOTE","DEFINITION","MECHANISM","CONTRAST"],
          "quality_need": "VERIFIED|OBSERVED|ANY",
          "priority": 1,
          "explanation_hints": ["string"]
        }
      ]
    }
  ]
}

Rules:
1) Language:
- All human-readable fields MUST be in language from the user payload.
- Do not mix languages inside one field.

2) Grounding and exploration:
- Use content_map as the primary grounding signal.
- Do NOT present unstated facts as true.
- If a useful angle is not explicitly confirmed in content_map, phrase it as a researchable check/question (exploratory framing), not as an asserted fact.
- Do not add specific names, dates, or numbers unless they are present in content_map.

3) Focus and anti-drift:
- Every branch must directly help answer root_question.
- Context/comparison branches are allowed only when they clearly return to the core topic.
- Avoid duplicate or near-duplicate branches.

4) Audience and adjacency:
- Prioritize branches using audience_profile.explain and audience_profile.level.
- Respect adjacency_policy: short, instrumental detours only.

5) Size adaptation by duration_target_seconds:
- <= 1500s: 4-5 top-level branches, 2-4 children each.
- 1501-3000s: 5-6 top-level branches, 3-5 children each.
- > 3000s: 6-7 top-level branches, 3-5 children each.

6) Priority semantics:
- 1 = must-have to answer root_question.
- 2 = important supporting context.
- 3 = optional but useful extension.
- 4+ = stretch context only.
- Keep branch priorities contiguous and meaningful.

7) quality_need semantics:
- VERIFIED for core factual/mechanistic/historical claims.
- OBSERVED for interpretive or less-certain claims that still need evidence.
- ANY only for speculative framing or open debate tails.

8) Researchability:
- Each question must be answerable via retrievable evidence (corpus/web), not pure opinion prompts.
- Child questions must be concrete sub-questions of their parent.

9) Hints quality:
- Keep explanation_hints concise and operational (what to explain/check), not narrative mini-essays.
- Prefer 1-3 hints per branch/child.

10) IDs and contract:
- Use ids in the format b1..bn and b1.1..b1.k without gaps.
- Return ONLY the JSON object matching the schema above.

Failure behavior:
- If content_map is sparse/noisy, return a minimal valid tree with grounded core branches plus exploratory checks.
- Never return empty branches.
"""


async def decompose(state: PipelineState) -> dict:
    """LangGraph node: build research tree and duration fill strategy."""
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, phase_started("phase2_decomposition", "Decomposing topic into research tree"))
    update_generation_progress(run_id, "phase2_decomposition", "decomposer", 10.0)

    async with async_session_factory() as db:
        # Load ContentMap
        content_map_data = {}
        if state.get("content_map_id"):
            result = await db.execute(
                select(ContentMap).where(ContentMap.id == uuid.UUID(state["content_map_id"]))
            )
            cm = result.scalar_one_or_none()
            if cm:
                content_map_data = cm.map_json

        # Load ShowBible
        bible_data = {}
        if state.get("show_bible_id"):
            result = await db.execute(
                select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"]))
            )
            bible = result.scalar_one_or_none()
            if bible:
                bible_data = {
                    "audience_profile": bible.audience_profile,
                    "adjacency_policy": bible.adjacency_policy,
                    "show_style": bible.show_style,
                }

        # Build research tree
        publish_event(run_id, step_started("phase2_decomposition", "build_tree", "Building research tree"))

        prompt_payload = {
            "topic": state["user_prompt"],
            "duration_target_seconds": state["duration_target"],
            "language": state["language"],
            "content_map": _compact_content_map(content_map_data),
            "audience_profile": bible_data.get("audience_profile", {}),
            "adjacency_policy": bible_data.get("adjacency_policy", ""),
            "show_style": bible_data.get("show_style", "Conversational"),
        }
        prompt = json.dumps(prompt_payload, ensure_ascii=False)

        raw = await call_sonnet_structured(
            system=DECOMPOSER_SYSTEM,
            prompt=prompt,
            temperature=0.6,
        )
        research_tree = parse_llm_json(raw)

        publish_event(run_id, step_completed("phase2_decomposition", "build_tree"))

        # Compute duration fill layers
        publish_event(run_id, step_started("phase2_decomposition", "duration_strategy", "Computing duration fill layers"))
        duration_minutes = state["duration_target"] / 60
        branch_count = len(research_tree.get("branches", []))
        fill_layers = compute_fill_layers(duration_minutes, branch_count, bible_data.get("audience_profile", {}))
        publish_event(run_id, step_completed("phase2_decomposition", "duration_strategy"))

        # Create or update EpisodeManifest for this run (idempotent resume/retry).
        run_uuid = uuid.UUID(run_id)
        existing_result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.generation_run_id == run_uuid)
        )
        manifest = existing_result.scalar_one_or_none()

        if manifest is None:
            manifest = EpisodeManifest(
                id=uuid.uuid4(),
                generation_run_id=run_uuid,
                research_tree_json=research_tree,
                duration_fill_layers=fill_layers,
            )
            db.add(manifest)
        else:
            manifest.research_tree_json = research_tree
            manifest.duration_fill_layers = fill_layers
            # Reset downstream planning fields when decomposition reruns.
            manifest.act_count = None
            manifest.acts_json = None

        await db.commit()
        manifest_id = str(manifest.id)

    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase2_decomposition", duration_ms))
    update_generation_progress(run_id, "phase2_decomposition", "complete", 15.0)

    branch_total = sum(1 + len(b.get("children", [])) for b in research_tree.get("branches", []))
    publish_event(run_id, progress(15.0, f"Research tree: {len(research_tree.get('branches', []))} angles, {branch_total} drill-downs"))

    return {"manifest_id": manifest_id, "current_phase": "phase2_decomposition"}


def _compact_content_map(content_map_data: Any) -> dict[str, Any]:
    """Keep content map signal while avoiding nested JSON strings and giant payloads."""
    if not isinstance(content_map_data, dict):
        return {}

    def _clip_text(value: Any, limit: int) -> str:
        return str(value or "")[:limit]

    compact_topics: list[dict[str, Any]] = []
    for raw in content_map_data.get("topics", [])[:8]:
        if not isinstance(raw, dict):
            continue
        compact_topics.append(
            {
                "name": _clip_text(raw.get("name"), 120),
                "description": _clip_text(raw.get("description"), 240),
                "chunk_count": raw.get("chunk_count"),
            }
        )

    compact_entities: list[dict[str, Any]] = []
    for raw in content_map_data.get("entities", [])[:12]:
        if not isinstance(raw, dict):
            continue
        compact_entities.append(
            {
                "name": _clip_text(raw.get("name"), 120),
                "type": _clip_text(raw.get("type"), 80),
                "mentions": raw.get("mentions"),
            }
        )

    compact_terms: list[dict[str, Any]] = []
    for raw in content_map_data.get("key_terms", [])[:12]:
        if not isinstance(raw, dict):
            continue
        compact_terms.append(
            {
                "term": _clip_text(raw.get("term"), 120),
                "context": _clip_text(raw.get("context"), 180),
            }
        )

    compact_gaps = [
        _clip_text(gap, 180)
        for gap in (content_map_data.get("gaps", []) if isinstance(content_map_data.get("gaps"), list) else [])
        if str(gap).strip()
    ][:8]

    compact: dict[str, Any] = {
        "summary": _clip_text(content_map_data.get("summary"), 1200),
        "topics": compact_topics,
        "entities": compact_entities,
        "key_terms": compact_terms,
        "gaps": compact_gaps,
    }

    if content_map_data.get("_raw_response"):
        compact["parse_warning"] = "content_map_parse_failed"

    return compact
