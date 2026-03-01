"""Phase 3.1: Hunter — closed-book retrieval from project corpus."""

import json
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.models.evidence import EvidenceSpan
from app.pipeline.state import PipelineState
from app.pipeline.phase3_research.tools import vector_search_tool, keyword_search_tool, get_chunk_content
from app.pipeline.llm_json import parse_llm_json
from app.services.llm import call_haiku_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, evidence_found, progress

logger = logging.getLogger(__name__)

RETRIEVAL_TOP_K = 40
RETRIEVAL_POOL_SIZE = 40
EXTRACTION_CHUNK_LIMIT = 8
DIVERSITY_SOURCE_PENALTY = 0.15
DIVERSITY_PARENT_PENALTY = 0.08

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
    all_asset_types_found: set[str] = set()

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
        branch_expected_types: dict[str, set[str]] = {}
        branch_found_types: dict[str, set[str]] = {}
        branch_meta: dict[str, dict] = {}

        for branch in branches:
            branch_id = branch["id"]
            expected_types = _normalize_asset_types(branch.get("expected_evidence_type", []))
            branch_expected_types.setdefault(branch_id, set()).update(expected_types)
            branch_found_types.setdefault(branch_id, set())
            branch_meta[branch_id] = {
                "question": branch["question"],
                "priority": branch.get("priority", 5),
            }
            questions.append({
                "id": branch_id,
                "branch_id": branch_id,
                "question": branch["question"],
                "expected_types": expected_types,
            })
            for child in branch.get("children", []):
                child_expected = _normalize_asset_types(child.get("expected_evidence_type", []))
                branch_expected_types.setdefault(branch_id, set()).update(child_expected)
                questions.append({
                    "id": child["id"],
                    "branch_id": branch_id,
                    "question": child["question"],
                    "expected_types": child_expected,
                })

        publish_event(run_id, step_started("phase3_research", "hunter_search", f"Searching {len(questions)} questions across {len(source_ids)} sources"))

        for i, q in enumerate(questions):
            # Hybrid search: vector + keyword, RRF merge
            vector_results = await vector_search_tool(q["question"], top_k=RETRIEVAL_TOP_K, source_ids=source_ids, db=db)
            keyword_results = await keyword_search_tool(q["question"], source_ids, db, top_k=RETRIEVAL_TOP_K)

            # RRF merge
            merged = _rrf_merge(vector_results, keyword_results, k=60)
            top_chunks = _select_diverse_chunks(
                merged,
                pool_size=RETRIEVAL_POOL_SIZE,
                limit=EXTRACTION_CHUNK_LIMIT,
                source_penalty=DIVERSITY_SOURCE_PENALTY,
                parent_penalty=DIVERSITY_PARENT_PENALTY,
            )

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
            }, ensure_ascii=False)

            try:
                raw = await call_haiku_structured(
                    system=EXTRACT_EVIDENCE_SYSTEM,
                    prompt=prompt,
                    max_tokens=2048,
                    temperature=0.2,
                )
                evidence_items = parse_llm_json(raw)
            except Exception:
                logger.warning("Evidence extraction failed for question %s", q["id"], exc_info=True)
                continue

            if not isinstance(evidence_items, list):
                continue

            # Create EvidenceSpan records
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue
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

                asset_type_value = str(item.get("asset_type", "mechanism")).lower()
                evidence = EvidenceSpan(
                    id=uuid.uuid4(),
                    project_id=uuid.UUID(state["project_id"]),
                    source_id=uuid.UUID(source_id),
                    chunk_id=uuid.UUID(chunk_id) if chunk_id else None,
                    verbatim_quote=item["verbatim_quote"],
                    asset_type=asset_type_value,
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

                asset_type_upper = asset_type_value.upper()
                if asset_type_upper:
                    branch_found_types.setdefault(q["branch_id"], set()).add(asset_type_upper)
                    all_asset_types_found.add(asset_type_upper)

            update_generation_progress(run_id, "phase3_research", "hunter", 20.0 + (i / len(questions)) * 10)

        await db.commit()

    research_gaps = []
    for branch in branches:
        branch_id = branch["id"]
        expected_assets = sorted(branch_expected_types.get(branch_id, set()))
        found_assets = sorted(branch_found_types.get(branch_id, set()))
        missing_assets = sorted([asset for asset in expected_assets if asset not in found_assets])
        if missing_assets:
            meta = branch_meta.get(branch_id, {})
            research_gaps.append({
                "branch_id": branch_id,
                "question": meta.get("question", branch.get("question", "")),
                "priority": meta.get("priority", branch.get("priority", 5)),
                "missing_assets": missing_assets,
                "expected_assets": expected_assets,
                "found_assets": found_assets,
            })

    publish_event(run_id, evidence_found(len(all_evidence_ids), sorted(all_asset_types_found)))
    publish_event(run_id, step_completed("phase3_research", "hunter_search"))
    publish_event(run_id, progress(30.0, f"Found {len(all_evidence_ids)} evidence spans in corpus"))

    return {
        "evidence_ids": all_evidence_ids,
        "research_gaps": research_gaps,
        "current_phase": "phase3_research",
    }


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
    merged = []
    for cid in sorted_ids:
        item = dict(items[cid])
        item["rrf_score"] = scores[cid]
        merged.append(item)
    return merged


def _normalize_asset_types(asset_types: list[str] | None) -> list[str]:
    if not asset_types:
        return []
    return [str(a).upper() for a in asset_types if isinstance(a, str) and a]


def _select_diverse_chunks(
    merged_results: list[dict],
    pool_size: int,
    limit: int,
    source_penalty: float,
    parent_penalty: float,
) -> list[dict]:
    """Greedy selection with diversity penalties for repeated source/parent."""
    pool = list(merged_results[:pool_size])
    if not pool:
        return []

    selected: list[dict] = []
    source_counts: dict[str, int] = {}
    parent_counts: dict[str, int] = {}

    while pool and len(selected) < limit:
        best_idx = -1
        best_score = float("-inf")

        for idx, candidate in enumerate(pool):
            source_id = candidate.get("source_id")
            parent_id = candidate.get("parent_chunk_id") or candidate.get("chunk_id")
            adjusted = float(candidate.get("rrf_score", 0.0))
            adjusted -= source_penalty * source_counts.get(source_id, 0)
            adjusted -= parent_penalty * parent_counts.get(parent_id, 0)

            if adjusted > best_score:
                best_score = adjusted
                best_idx = idx

        chosen = pool.pop(best_idx)
        selected.append(chosen)

        source_id = chosen.get("source_id")
        parent_id = chosen.get("parent_chunk_id") or chosen.get("chunk_id")
        source_counts[source_id] = source_counts.get(source_id, 0) + 1
        parent_counts[parent_id] = parent_counts.get(parent_id, 0) + 1

    return selected
