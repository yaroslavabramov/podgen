"""Phase 2: Query Decomposition — Research Tree + Duration Fill Strategy."""

import json
import logging
import time
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models.content_map import ContentMap
from app.models.episode_manifest import EpisodeManifest
from app.models.show_bible import ShowBible
from app.pipeline.state import PipelineState
from app.pipeline.phase2_decomposition.duration_strategy import compute_fill_layers
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, phase_completed, step_started, step_completed, progress

logger = logging.getLogger(__name__)

DECOMPOSER_SYSTEM = """You are a research decomposer for a long-form podcast.
Given a topic, content map, audience profile, and adjacency policy, build a Research Tree.

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
          "intent": "string",
          "expected_evidence_type": ["string"],
          "quality_need": "string",
          "priority": 1,
          "explanation_hints": []
        }
      ]
    }
  ]
}

Generate 5-7 top-level branches with 3-5 children each.
Prioritize branches based on audience knowledge gaps.
Mark quality_need as VERIFIED for core factual claims."""


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

        prompt = json.dumps({
            "topic": state["user_prompt"],
            "duration_target_seconds": state["duration_target"],
            "language": state["language"],
            "content_map_summary": json.dumps(content_map_data)[:3000],
            "audience_profile": bible_data.get("audience_profile", {}),
            "adjacency_policy": bible_data.get("adjacency_policy", ""),
            "show_style": bible_data.get("show_style", "Conversational"),
        })

        raw = await call_sonnet_structured(
            system=DECOMPOSER_SYSTEM,
            prompt=prompt,
            max_tokens=4096,
            temperature=0.6,
        )
        research_tree = json.loads(raw)

        publish_event(run_id, step_completed("phase2_decomposition", "build_tree"))

        # Compute duration fill layers
        publish_event(run_id, step_started("phase2_decomposition", "duration_strategy", "Computing duration fill layers"))
        duration_minutes = state["duration_target"] / 60
        branch_count = len(research_tree.get("branches", []))
        fill_layers = compute_fill_layers(duration_minutes, branch_count, bible_data.get("audience_profile", {}))
        publish_event(run_id, step_completed("phase2_decomposition", "duration_strategy"))

        # Create EpisodeManifest
        manifest = EpisodeManifest(
            id=uuid.uuid4(),
            generation_run_id=uuid.UUID(run_id),
            research_tree_json=research_tree,
            duration_fill_layers=fill_layers,
        )
        db.add(manifest)
        await db.commit()
        manifest_id = str(manifest.id)

    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase2_decomposition", duration_ms))
    update_generation_progress(run_id, "phase2_decomposition", "complete", 15.0)

    branch_total = sum(1 + len(b.get("children", [])) for b in research_tree.get("branches", []))
    publish_event(run_id, progress(15.0, f"Research tree: {len(research_tree.get('branches', []))} angles, {branch_total} drill-downs"))

    return {"manifest_id": manifest_id, "current_phase": "phase2_decomposition"}
