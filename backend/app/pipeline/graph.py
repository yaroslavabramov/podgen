"""Main LangGraph pipeline assembly."""

import logging
import uuid

from langgraph.graph import StateGraph, END
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_run import GenerationRun
from app.models.source import Source
from app.models.content_map import ContentMap
from app.pipeline.state import PipelineState

# Phase imports
from app.pipeline.phase0_setup.show_configurator import setup_show
from app.pipeline.phase2_decomposition.decomposer import decompose
from app.pipeline.phase3_research.hunter import hunt_closed_book
from app.pipeline.phase3_research.librarian_fast import research_fast_web
from app.pipeline.phase4_claims.claim_builder import build_claims
from app.pipeline.phase4_claims.verifier import verify_claims
from app.pipeline.phase4_claims.conflict_resolver import resolve_conflicts
from app.pipeline.phase5_planning.showrunner import plan_episode
from app.pipeline.phase5_planning.act_planner import plan_act
from app.pipeline.phase5_planning.integrator import integrate_acts
from app.pipeline.phase6_writing.screenwriter import write_screenplay
from app.pipeline.phase6_writing.writer_revolt import evaluate_and_revolt
from app.pipeline.phase6_writing.citation_auditor import audit_citations
from app.pipeline.phase7_audio.tts import generate_audio
from app.pipeline.phase7_audio.shownotes import build_shownotes

logger = logging.getLogger(__name__)


def _should_loop_verify(state: PipelineState) -> str:
    """Check if verifier needs to loop back to research."""
    counts = state.get("iteration_counts", {})
    verify_loops = counts.get("verify_research_loops", 0)
    needs_research = counts.get("verify_needs_research", False)
    if needs_research and verify_loops < 2:
        return "research_fast_web"
    return "resolve_conflicts"


def _should_revolt(state: PipelineState) -> str:
    """Check if writer revolt should trigger rewrite."""
    quality = state.get("quality_signals", {})
    counts = state.get("iteration_counts", {})
    revolt_count = counts.get("writer_planner_loops", 0)
    needs_rewrite = quality.get("revolt_triggered", False)
    if needs_rewrite and revolt_count <= 4:
        return "write_screenplay"
    return "audit_citations"


def build_graph() -> StateGraph:
    """Build the LangGraph pipeline."""
    graph = StateGraph(PipelineState)

    # Add nodes
    graph.add_node("setup_show", setup_show)
    graph.add_node("decompose", decompose)
    graph.add_node("hunt_closed_book", hunt_closed_book)
    graph.add_node("research_fast_web", research_fast_web)
    graph.add_node("build_claims", build_claims)
    graph.add_node("verify_claims", verify_claims)
    graph.add_node("resolve_conflicts", resolve_conflicts)
    graph.add_node("plan_episode", plan_episode)
    graph.add_node("plan_acts", plan_act)
    graph.add_node("integrate_acts", integrate_acts)
    graph.add_node("write_screenplay", write_screenplay)
    graph.add_node("evaluate_and_revolt", evaluate_and_revolt)
    graph.add_node("audit_citations", audit_citations)
    graph.add_node("generate_audio", generate_audio)
    graph.add_node("build_shownotes", build_shownotes)

    # Define edges
    graph.set_entry_point("setup_show")

    graph.add_edge("setup_show", "decompose")
    graph.add_edge("decompose", "hunt_closed_book")
    graph.add_edge("hunt_closed_book", "research_fast_web")
    graph.add_edge("research_fast_web", "build_claims")
    graph.add_edge("build_claims", "verify_claims")

    # Verify can loop to research or proceed
    graph.add_conditional_edges("verify_claims", _should_loop_verify, {
        "research_fast_web": "research_fast_web",
        "resolve_conflicts": "resolve_conflicts",
    })

    graph.add_edge("resolve_conflicts", "plan_episode")
    graph.add_edge("plan_episode", "plan_acts")
    graph.add_edge("plan_acts", "integrate_acts")
    graph.add_edge("integrate_acts", "write_screenplay")
    graph.add_edge("write_screenplay", "evaluate_and_revolt")

    # Revolt can loop back to screenwriter or proceed to audit
    graph.add_conditional_edges("evaluate_and_revolt", _should_revolt, {
        "write_screenplay": "write_screenplay",
        "audit_citations": "audit_citations",
    })

    graph.add_edge("audit_citations", "generate_audio")
    graph.add_edge("generate_audio", "build_shownotes")
    graph.add_edge("build_shownotes", END)

    return graph


# Compiled graph (singleton)
_compiled_graph = None


def get_compiled_graph():
    global _compiled_graph
    if _compiled_graph is None:
        graph = build_graph()
        _compiled_graph = graph.compile()
    return _compiled_graph


async def execute_pipeline(run: GenerationRun, db: AsyncSession, resume: bool = False) -> str:
    """Execute the full pipeline for a generation run. Returns episode_id."""
    compiled = get_compiled_graph()

    # Build initial state
    config = run.config or {}

    # Get source IDs for project
    result = await db.execute(
        select(Source.id).where(
            Source.project_id == run.project_id,
            Source.is_temporary == False,
            Source.ingestion_status == "completed",
        )
    )
    source_ids = [str(row[0]) for row in result.fetchall()]

    # Get content map if exists
    result = await db.execute(
        select(ContentMap.id).where(ContentMap.project_id == run.project_id).order_by(ContentMap.created_at.desc())
    )
    content_map_row = result.first()
    content_map_id = str(content_map_row[0]) if content_map_row else ""

    initial_state: PipelineState = {
        "generation_run_id": str(run.id),
        "project_id": str(run.project_id),
        "user_id": str(run.user_id),
        "user_prompt": config.get("user_prompt", ""),
        "duration_target": config.get("duration_target", 1800),
        "language": config.get("language", "en"),
        "show_bible_id": str(run.show_bible_id),
        "source_ids": source_ids,
        "content_map_id": content_map_id,
        "manifest_id": "",
        "evidence_ids": [],
        "claim_ids": [],
        "conflict_brief_ids": [],
        "script_segment_ids": [],
        "episode_id": "",
        "current_phase": "",
        "iteration_counts": {},
        "quality_signals": {},
    }

    # Execute graph
    logger.info("Starting pipeline for generation run %s", run.id)
    final_state = None

    async for state in compiled.astream(initial_state):
        final_state = state
        # Update run progress in DB
        if isinstance(state, dict):
            for node_output in state.values():
                if isinstance(node_output, dict) and node_output.get("current_phase"):
                    run.current_phase = node_output["current_phase"]
                    await db.commit()

    # Extract episode_id from final state
    if final_state:
        for node_output in final_state.values():
            if isinstance(node_output, dict) and node_output.get("episode_id"):
                return node_output["episode_id"]

    raise RuntimeError("Pipeline completed but no episode_id produced")
