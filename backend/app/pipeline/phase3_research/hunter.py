"""Phase 3.1: Hunter — closed-book retrieval from project corpus."""

import json
import logging
import time
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.models.evidence import EvidenceSpan
from app.models.source import Source
from app.pipeline.state import PipelineState
from app.pipeline.phase3_research.tools import vector_search_tool, keyword_search_tool, get_chunk_content
from app.services.llm import call_haiku_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, evidence_found, progress

logger = logging.getLogger(__name__)

EXTRACT_EVIDENCE_SYSTEM = """You are an evidence extractor. Given a chunk of text and a research question,
extract relevant evidence spans.

Return JSON array:
[
  {
    "verbatim_quote": "exact quote from the text",
    "asset_type": "STATISTIC|ANECDOTE|EXPERT_QUOTE|DEFINITION|MECHANISM|CASE|TIMELINE|CONTRAST",
    "narrative_value_score": 0.0-1.0,
    "tellability_notes": "why this is interesting for a podcast",
    "locator": {"type": "section", "value": "heading path"}
  }
]

Only extract genuinely relevant evidence. Return [] if nothing relevant found."""


async def hunt_closed_book(state: PipelineState) -> dict:
    """LangGraph node: search project corpus for evidence matching research tree."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase3_research", "Searching project corpus for evidence"))
    update_generation_progress(run_id, "phase3_research", "hunter", 20.0)

    source_ids = state.get("source_ids", [])
    if not source_ids:
        return {"evidence_ids": [], "current_phase": "phase3_research"}

    all_evidence_ids = list(state.get("evidence_ids", []))

    async with async_session_factory() as db:
        # Load research tree
        manifest = None
        if state.get("manifest_id"):
            result = await db.execute(
                select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
            )
            manifest = result.scalar_one_or_none()

        if not manifest or not manifest.research_tree_json:
            return {"evidence_ids": all_evidence_ids, "current_phase": "phase3_research"}

        tree = manifest.research_tree_json
        branches = tree.get("branches", [])

        # Flatten all questions (branches + children)
        questions = []
        for branch in branches:
            questions.append({"id": branch["id"], "question": branch["question"], "expected_types": branch.get("expected_evidence_type", [])})
            for child in branch.get("children", []):
                questions.append({"id": child["id"], "question": child["question"], "expected_types": child.get("expected_evidence_type", [])})

        publish_event(run_id, step_started("phase3_research", "hunter_search", f"Searching {len(questions)} questions across {len(source_ids)} sources"))

        for i, q in enumerate(questions):
            # Hybrid search: vector + keyword, RRF merge
            vector_results = await vector_search_tool(q["question"], top_k=10, source_ids=source_ids, db=db)
            keyword_results = await keyword_search_tool(q["question"], source_ids, db)

            # RRF merge
            merged = _rrf_merge(vector_results, keyword_results, k=60)
            top_chunks = merged[:5]

            if not top_chunks:
                continue

            # Load full content for top chunks
            chunk_texts = []
            for hit in top_chunks:
                content = await get_chunk_content(hit["chunk_id"], db)
                if content:
                    chunk_texts.append(content)

            if not chunk_texts:
                continue

            # Extract evidence using Haiku
            prompt = json.dumps({
                "research_question": q["question"],
                "expected_evidence_types": q["expected_types"],
                "chunks": [{"content": c["content"][:2000], "source": c.get("source_title", ""), "heading": c.get("heading_path", [])} for c in chunk_texts],
            })

            try:
                raw = await call_haiku_structured(
                    system=EXTRACT_EVIDENCE_SYSTEM,
                    prompt=prompt,
                    max_tokens=2048,
                    temperature=0.2,
                )
                evidence_items = json.loads(raw)
            except Exception:
                logger.warning("Evidence extraction failed for question %s", q["id"], exc_info=True)
                continue

            # Create EvidenceSpan records
            for item in evidence_items:
                if not item.get("verbatim_quote"):
                    continue

                # Find the source_id from the chunk that contains this quote
                source_id = chunk_texts[0].get("source_id") if chunk_texts else source_ids[0] if source_ids else None
                if not source_id:
                    continue

                # Find matching chunk_id
                chunk_id = None
                for ct in chunk_texts:
                    if item["verbatim_quote"][:50] in ct.get("content", ""):
                        chunk_id = ct.get("chunk_id")
                        source_id = ct.get("source_id", source_id)
                        break

                evidence = EvidenceSpan(
                    id=uuid.uuid4(),
                    project_id=uuid.UUID(state["project_id"]),
                    source_id=uuid.UUID(source_id),
                    chunk_id=uuid.UUID(chunk_id) if chunk_id else None,
                    verbatim_quote=item["verbatim_quote"],
                    asset_type=item.get("asset_type", "mechanism").lower(),
                    narrative_value_score=item.get("narrative_value_score", 0.5),
                    tellability_notes=item.get("tellability_notes"),
                    locator=item.get("locator"),
                    reliability_hint=next(
                        (ct.get("domain_reliability") for ct in chunk_texts if ct.get("chunk_id") == chunk_id),
                        None,
                    ),
                )
                db.add(evidence)
                await db.flush()
                all_evidence_ids.append(str(evidence.id))

            update_generation_progress(run_id, "phase3_research", "hunter", 20.0 + (i / len(questions)) * 10)

        await db.commit()

    asset_types = list(set(e.get("asset_type", "") for e in evidence_items)) if 'evidence_items' in dir() else []
    publish_event(run_id, evidence_found(len(all_evidence_ids), asset_types))
    publish_event(run_id, step_completed("phase3_research", "hunter_search"))
    publish_event(run_id, progress(30.0, f"Found {len(all_evidence_ids)} evidence spans in corpus"))

    return {"evidence_ids": all_evidence_ids, "current_phase": "phase3_research"}


def _rrf_merge(vector_results: list[dict], keyword_results: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion merge of two result lists."""
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for rank, item in enumerate(vector_results):
        cid = item["chunk_id"]
        scores[cid] = scores.get(cid, 0) + 1 / (k + rank + 1)
        items[cid] = item

    for rank, item in enumerate(keyword_results):
        cid = item["chunk_id"]
        scores[cid] = scores.get(cid, 0) + 1 / (k + rank + 1)
        items[cid] = item

    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    return [items[cid] for cid in sorted_ids]
