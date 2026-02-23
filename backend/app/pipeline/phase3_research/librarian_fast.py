"""Phase 3.2A: Librarian Fast — open-web evidence search (non-blocking)."""

import json
import logging
import time
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.models.evidence import EvidenceSpan
from app.models.source import Source
from app.pipeline.state import PipelineState
from app.pipeline.phase3_research.tools import web_search_tool, fetch_page_tool, create_web_evidence
from app.pipeline.phase3_research.librarian_deep import trigger_deep_ingestion
from app.services.llm import call_sonnet_structured
from app.config import settings
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import step_started, step_completed, evidence_found, progress

logger = logging.getLogger(__name__)

EXTRACT_WEB_EVIDENCE_SYSTEM = """You are a web evidence extractor for a podcast research pipeline.
Given web search results and a research question, identify the most valuable evidence.

Return JSON array:
[
  {
    "url": "source URL",
    "verbatim_quote": "exact quote from the content",
    "asset_type": "STATISTIC|ANECDOTE|EXPERT_QUOTE|DEFINITION|MECHANISM|CASE|TIMELINE|CONTRAST",
    "narrative_value_score": 0.0-1.0,
    "tellability_notes": "why this evidence is valuable for a podcast"
  }
]

Max 3 evidence items per question. Only extract genuinely valuable evidence."""


async def research_fast_web(state: PipelineState) -> dict:
    """LangGraph node: fast web research for evidence gaps."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase3_research", "fast_web", "Searching open web for additional evidence"))

    all_evidence_ids = list(state.get("evidence_ids", []))
    iteration_counts = dict(state.get("iteration_counts", {}))
    fast_web_count = iteration_counts.get("fast_web_per_act", 0)

    # Governor: max web sources per act
    if fast_web_count >= settings.max_fast_web_per_act:
        logger.info("Fast web governor hit: %d sources used", fast_web_count)
        return {"evidence_ids": all_evidence_ids, "iteration_counts": iteration_counts}

    async with async_session_factory() as db:
        # Load research tree to find gaps
        if not state.get("manifest_id"):
            return {"evidence_ids": all_evidence_ids}

        result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
        )
        manifest = result.scalar_one_or_none()
        if not manifest or not manifest.research_tree_json:
            return {"evidence_ids": all_evidence_ids}

        tree = manifest.research_tree_json
        branches = tree.get("branches", [])

        # Find branches with insufficient evidence (simple heuristic)
        evidence_per_branch = len(all_evidence_ids) / max(len(branches), 1)
        gap_branches = [b for b in branches if b.get("priority", 5) <= 2][:3]

        if not gap_branches:
            gap_branches = branches[:2]

        new_evidence_count = 0

        for branch in gap_branches:
            if fast_web_count >= settings.max_fast_web_per_act:
                break

            query = branch["question"]

            try:
                # Web search
                search_results = await web_search_tool(query)
                if not search_results:
                    continue

                # Fetch top result pages
                page_contents = []
                for sr in search_results[:2]:
                    try:
                        page = await fetch_page_tool(sr["url"])
                        if page and not page.get("error"):
                            page_contents.append({
                                "url": sr["url"],
                                "title": sr["title"],
                                "content": page.get("content_markdown", "")[:5000],
                            })
                    except Exception:
                        continue

                if not page_contents:
                    continue

                # Extract evidence with Sonnet
                prompt = json.dumps({
                    "research_question": query,
                    "web_pages": page_contents,
                })

                raw = await call_sonnet_structured(
                    system=EXTRACT_WEB_EVIDENCE_SYSTEM,
                    prompt=prompt,
                    max_tokens=2048,
                    temperature=0.3,
                )
                evidence_items = json.loads(raw)

                for item in evidence_items:
                    if not item.get("verbatim_quote") or not item.get("url"):
                        continue

                    eid = await create_web_evidence(
                        url=item["url"],
                        quote=item["verbatim_quote"],
                        asset_type=item.get("asset_type", "mechanism").lower(),
                        project_id=state["project_id"],
                        db=db,
                        generation_run_id=state["generation_run_id"],
                    )
                    all_evidence_ids.append(eid)
                    new_evidence_count += 1
                    fast_web_count += 1

            except Exception:
                logger.warning("Fast web search failed for: %s", query, exc_info=True)
                continue

        await db.commit()

    # Trigger deep ingestion for substantial temp sources (long articles, PDFs)
    pending_deep = list(state.get("pending_deep_ingestion_job_ids", []))

    # Check which pending jobs are still running to get real concurrency count
    from celery.result import AsyncResult
    from app.workers.celery_app import celery_app as celery

    still_running = []
    for task_id in pending_deep:
        result = AsyncResult(task_id, app=celery)
        if result.state in ("PENDING", "STARTED", "RETRY"):
            still_running.append(task_id)
    deep_active = len(still_running)
    pending_deep = still_running  # prune completed/failed

    if deep_active < settings.max_deep_ingestion_concurrent:
        from app.models.source import IngestionStatus
        async with async_session_factory() as db2:
            deep_result = await db2.execute(
                select(Source).where(
                    Source.project_id == uuid.UUID(state["project_id"]),
                    Source.is_temporary == True,
                    Source.fully_ingested == False,
                    Source.generation_run_id == uuid.UUID(state["generation_run_id"]),
                    # Exclude sources already being processed (prevents duplicate tasks)
                    Source.ingestion_status != IngestionStatus.PROCESSING,
                    Source.ingestion_status != IngestionStatus.COMPLETED,
                )
            )
            deep_candidates = deep_result.scalars().all()

            # Mark selected candidates as PROCESSING atomically before enqueuing,
            # so the next pass of research_fast_web won't pick them up again.
            for src in deep_candidates:
                if deep_active >= settings.max_deep_ingestion_concurrent:
                    break
                src.ingestion_status = IngestionStatus.PROCESSING
                deep_active += 1
            await db2.commit()

        # Now enqueue Celery tasks for the sources we just marked
        for src in deep_candidates:
            if src.ingestion_status != IngestionStatus.PROCESSING:
                continue  # wasn't selected (over concurrency limit)
            try:
                task_id = trigger_deep_ingestion(str(src.id), run_id)
                pending_deep.append(task_id)
            except Exception:
                logger.warning("Failed to trigger deep ingestion for source %s", src.id)

    iteration_counts["deep_ingestion_active"] = deep_active
    iteration_counts["fast_web_per_act"] = fast_web_count
    publish_event(run_id, step_completed("phase3_research", "fast_web"))
    if new_evidence_count > 0:
        publish_event(run_id, evidence_found(new_evidence_count, ["web"]))
        publish_event(run_id, progress(33.0, f"Found {new_evidence_count} additional web evidence"))

    return {
        "evidence_ids": all_evidence_ids,
        "pending_deep_ingestion_job_ids": pending_deep,
        "iteration_counts": iteration_counts,
        "current_phase": "phase3_research",
    }
