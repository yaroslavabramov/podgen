"""Main LangGraph pipeline assembly."""

import logging
import uuid
from functools import wraps
from typing import Any

from langgraph.errors import EmptyInputError
from langgraph.graph import StateGraph, END
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.generation_run import GenerationRun
from app.models.source import Source
from app.models.content_map import ContentMap
from app.models.episode import Episode
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
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope

logger = logging.getLogger(__name__)

_NODE_PHASES = {
    "setup_show": "phase0_setup",
    "decompose": "phase2_decomposition",
    "hunt_closed_book": "phase3_research",
    "research_fast_web": "phase3_research",
    "build_claims": "phase4_claims",
    "verify_claims": "phase4_claims",
    "resolve_conflicts": "phase4_claims",
    "plan_episode": "phase5_planning",
    "plan_acts": "phase5_planning",
    "integrate_acts": "phase5_planning",
    "write_screenplay": "phase6_writing",
    "evaluate_and_revolt": "phase6_writing",
    "audit_citations": "phase6_writing",
    "generate_audio": "phase7_audio",
    "build_shownotes": "phase7_audio",
}


def _summarize_state(state: PipelineState) -> dict[str, Any]:
    return {
        "generation_run_id": state.get("generation_run_id"),
        "project_id": state.get("project_id"),
        "language": state.get("language"),
        "duration_target": state.get("duration_target"),
        "current_phase": state.get("current_phase"),
        "source_count": len(state.get("source_ids", [])),
        "evidence_count": len(state.get("evidence_ids", [])),
        "claim_count": len(state.get("claim_ids", [])),
        "conflict_brief_count": len(state.get("conflict_brief_ids", [])),
        "script_segment_count": len(state.get("script_segment_ids", [])),
        "iteration_keys": sorted((state.get("iteration_counts") or {}).keys()),
        "quality_signal_keys": sorted((state.get("quality_signals") or {}).keys()),
    }


def _summarize_update(update: Any) -> dict[str, Any]:
    if not isinstance(update, dict):
        return {"result_type": type(update).__name__}

    summary: dict[str, Any] = {"keys": sorted(update.keys())}
    for key, value in update.items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            summary[f"{key}_keys"] = sorted(value.keys())
        elif isinstance(value, str):
            if key.endswith("_id") or key == "current_phase":
                summary[key] = value
            else:
                summary[f"{key}_chars"] = len(value)
        else:
            summary[key] = value
    return summary


def _trace_node(node_name: str, node_fn: Any) -> Any:
    phase = _NODE_PHASES.get(node_name, "unknown")

    @wraps(node_fn)
    async def _wrapped(state: PipelineState) -> dict:
        trace_ctx = {
            "component": "pipeline.node",
            "phase": phase,
            "node": node_name,
            "generation_run_id": state.get("generation_run_id"),
            "project_id": state.get("project_id"),
            "language": state.get("language"),
        }
        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["pipeline", f"phase:{phase}", f"node:{node_name}"]):
                with trace_span(
                    f"pipeline.node.{node_name}",
                    run_type="chain",
                    inputs=_summarize_state(state),
                    metadata={"phase": phase, "node": node_name},
                    tags=["pipeline-node"],
                ) as run_tree:
                    result = await node_fn(state)
                    safe_set_run(run_tree, outputs=_summarize_update(result))
                    return result

    return _wrapped


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
    graph.add_node("setup_show", _trace_node("setup_show", setup_show))
    graph.add_node("decompose", _trace_node("decompose", decompose))
    graph.add_node("hunt_closed_book", _trace_node("hunt_closed_book", hunt_closed_book))
    graph.add_node("research_fast_web", _trace_node("research_fast_web", research_fast_web))
    graph.add_node("build_claims", _trace_node("build_claims", build_claims))
    graph.add_node("verify_claims", _trace_node("verify_claims", verify_claims))
    graph.add_node("resolve_conflicts", _trace_node("resolve_conflicts", resolve_conflicts))
    graph.add_node("plan_episode", _trace_node("plan_episode", plan_episode))
    graph.add_node("plan_acts", _trace_node("plan_acts", plan_act))
    graph.add_node("integrate_acts", _trace_node("integrate_acts", integrate_acts))
    graph.add_node("write_screenplay", _trace_node("write_screenplay", write_screenplay))
    graph.add_node("evaluate_and_revolt", _trace_node("evaluate_and_revolt", evaluate_and_revolt))
    graph.add_node("audit_citations", _trace_node("audit_citations", audit_citations))
    graph.add_node("generate_audio", _trace_node("generate_audio", generate_audio))
    graph.add_node("build_shownotes", _trace_node("build_shownotes", build_shownotes))

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


_compiled_graph = None
_checkpointer_schema_ready = False


def get_compiled_graph(checkpointer: Any | None = None):
    if checkpointer is None:
        global _compiled_graph
        if _compiled_graph is None:
            _compiled_graph = build_graph().compile()
        return _compiled_graph
    # Checkpointer holds loop-bound async resources; compile per execution.
    return build_graph().compile(checkpointer=checkpointer)


def _checkpoint_conn_string() -> str:
    return settings.database_url_sync_resolved


async def _ensure_checkpointer_schema(checkpointer: Any) -> None:
    global _checkpointer_schema_ready
    if _checkpointer_schema_ready:
        return
    await checkpointer.setup()
    _checkpointer_schema_ready = True


def _build_initial_state(run: GenerationRun, source_ids: list[str], content_map_id: str) -> PipelineState:
    config = run.config or {}
    return {
        "generation_run_id": str(run.id),
        "project_id": str(run.project_id),
        "user_id": str(run.user_id),
        "user_prompt": config.get("user_prompt", ""),
        "duration_target": config.get("duration_target", 1800),
        "language": config.get("language", "en"),
        "show_bible_id": str(run.show_bible_id) if run.show_bible_id else "",
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


def _extract_episode_id(final_state: dict[str, Any] | None) -> str | None:
    if not final_state:
        return None
    for node_output in final_state.values():
        if isinstance(node_output, dict):
            episode_id = node_output.get("episode_id")
            if isinstance(episode_id, str) and episode_id:
                return episode_id
    return None


def _transcript_has_lines(transcript_json: Any) -> bool:
    if not isinstance(transcript_json, list) or not transcript_json:
        return False
    for line in transcript_json:
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if isinstance(text, str) and text.strip():
            return True
    return False


def _assert_episode_complete(episode: Episode) -> None:
    issues: list[str] = []
    if not episode.audio_file_path:
        issues.append("missing audio file path")
    if episode.duration_seconds <= 0:
        issues.append("duration is zero")
    if not _transcript_has_lines(episode.transcript_json):
        issues.append("transcript is empty")
    if issues:
        raise RuntimeError(
            "Pipeline finished with incomplete episode artifacts: "
            + ", ".join(issues)
        )


async def _resolve_episode_id(
    run: GenerationRun,
    db: AsyncSession,
    final_state: dict[str, Any] | None,
    resume: bool,
) -> str:
    candidate_episode_id = _extract_episode_id(final_state)
    episode: Episode | None = None

    if candidate_episode_id:
        try:
            episode_uuid = uuid.UUID(candidate_episode_id)
        except ValueError as exc:
            raise RuntimeError(f"Pipeline returned invalid episode_id: {candidate_episode_id}") from exc

        result = await db.execute(
            select(Episode).where(
                Episode.id == episode_uuid,
                Episode.generation_run_id == run.id,
            )
        )
        episode = result.scalar_one_or_none()
        if episode is None:
            raise RuntimeError(
                "Pipeline returned episode_id that does not belong to this generation run"
            )
    elif resume:
        # Resume may replay from checkpoints where terminal payload is omitted.
        result = await db.execute(select(Episode).where(Episode.generation_run_id == run.id))
        episode = result.scalar_one_or_none()
        if episode is None:
            raise RuntimeError("Pipeline completed but no episode_id produced")
    else:
        raise RuntimeError("Pipeline completed but no episode_id produced")

    _assert_episode_complete(episode)
    return str(episode.id)


async def execute_pipeline(run: GenerationRun, db: AsyncSession, resume: bool = False) -> str:
    """Execute the full pipeline for a generation run. Returns episode_id."""
    trace_ctx = {
        "component": "pipeline.execute",
        "generation_run_id": str(run.id),
        "project_id": str(run.project_id),
        "user_id": str(run.user_id),
        "language": (run.config or {}).get("language", "en"),
    }

    with push_trace_context(trace_ctx):
        with tracing_scope(metadata=trace_ctx, tags=["pipeline", "generation"]):
            with trace_span(
                "pipeline.execute",
                run_type="chain",
                inputs={"generation_run_id": str(run.id), "resume": resume},
                metadata={
                    "thread_id": run.langgraph_thread_id or str(run.id),
                    "has_show_bible": bool(run.show_bible_id),
                },
                tags=["pipeline-root"],
            ) as pipeline_run:
                # Get source IDs for project
                result = await db.execute(
                    select(Source.id).where(
                        Source.project_id == run.project_id,
                        Source.is_temporary.is_(False),
                        Source.ingestion_status == "completed",
                    )
                )
                source_ids = [str(row[0]) for row in result.fetchall()]

                # Get content map if exists
                result = await db.execute(
                    select(ContentMap.id)
                    .where(ContentMap.project_id == run.project_id)
                    .order_by(ContentMap.created_at.desc())
                )
                content_map_row = result.first()
                content_map_id = str(content_map_row[0]) if content_map_row else ""

                initial_state = _build_initial_state(run, source_ids, content_map_id)
                thread_id = run.langgraph_thread_id or str(run.id)
                run.langgraph_thread_id = thread_id
                graph_config = {"configurable": {"thread_id": thread_id}}

                logger.info(
                    "Starting pipeline for generation run %s (resume=%s, thread_id=%s)",
                    run.id,
                    resume,
                    thread_id,
                )
                await db.commit()

                try:
                    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                except ImportError as exc:
                    raise RuntimeError(
                        "LangGraph checkpoint backend requires psycopg with libpq support. "
                        "Install `psycopg[binary]` in backend env and restart worker."
                    ) from exc

                async with AsyncPostgresSaver.from_conn_string(_checkpoint_conn_string()) as checkpointer:
                    await _ensure_checkpointer_schema(checkpointer)

                    if resume:
                        saved = await checkpointer.aget_tuple(graph_config)
                        if saved is None:
                            raise RuntimeError(
                                "Resume is unavailable for this run: checkpoint state not found. "
                                "Start a new generation run."
                            )

                    compiled = get_compiled_graph(checkpointer=checkpointer)
                    graph_input = None if resume else initial_state
                    final_state = None

                    try:
                        async for state in compiled.astream(graph_input, config=graph_config):
                            final_state = state
                            if isinstance(state, dict):
                                for node_output in state.values():
                                    if isinstance(node_output, dict) and node_output.get("current_phase"):
                                        run.current_phase = node_output["current_phase"]
                                        await db.commit()
                    except EmptyInputError as exc:
                        if resume:
                            raise RuntimeError(
                                "Resume is unavailable for this run: no resumable checkpoint. "
                                "Start a new generation run."
                            ) from exc
                        raise

                episode_id = await _resolve_episode_id(run, db, final_state, resume)
                safe_set_run(
                    pipeline_run,
                    outputs={
                        "episode_id": episode_id,
                        "source_count": len(source_ids),
                        "content_map_id": content_map_id,
                        "thread_id": thread_id,
                        "resume": resume,
                    },
                )
                return episode_id
