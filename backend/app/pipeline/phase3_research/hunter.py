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

_ALLOWED_EVIDENCE_TYPES = {
    "TIMELINE",
    "CASE",
    "EXPERT_QUOTE",
    "STATISTIC",
    "ANECDOTE",
    "DEFINITION",
    "MECHANISM",
    "CONTRAST",
}

EXTRACT_EVIDENCE_SYSTEM = """You are an evidence extractor. Given a chunk of text and a research question,
extract relevant evidence spans.

Input may include:
- required_evidence: strict evidence types that must be found if present in the text.
- expected_evidence_types / target_evidence_types: useful but non-blocking evidence targets.
Prioritize required_evidence first, then expected/target evidence types.

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
    new_asset_types_found: set[str] = set()
    new_evidence_count = 0

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
        question_required_assets: dict[str, dict[str, int]] = {}
        question_expected_types: dict[str, set[str]] = {}
        question_found_counts: dict[str, dict[str, int]] = {}

        for branch in branches:
            branch_id = branch["id"]
            required_targets, expected_types = _extract_node_evidence_contract(branch)
            _merge_required_assets(question_required_assets, branch_id, required_targets)
            question_expected_types.setdefault(branch_id, set()).update(expected_types)
            question_found_counts.setdefault(branch_id, {})
            questions.append({
                "id": branch_id,
                "branch_id": branch_id,
                "question": branch["question"],
                "priority": branch.get("priority", 5),
                "required_targets": required_targets,
                "expected_types": expected_types,
            })
            for child in branch.get("children", []):
                child_required, child_expected = _extract_node_evidence_contract(child)
                _merge_required_assets(question_required_assets, child["id"], child_required)
                question_expected_types.setdefault(child["id"], set()).update(child_expected)
                question_found_counts.setdefault(child["id"], {})
                questions.append({
                    "id": child["id"],
                    "branch_id": branch_id,
                    "question": child["question"],
                    "priority": child.get("priority", branch.get("priority", 5)),
                    "required_targets": child_required,
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
                "required_evidence": q["required_targets"],
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
            chunk_by_id = {
                str(ct.get("chunk_id")): ct
                for ct in chunk_texts
                if ct.get("chunk_id")
            }
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue
                if not item.get("verbatim_quote"):
                    continue

                normalized_asset_type = _normalize_single_asset_type(item.get("asset_type"))
                if normalized_asset_type is None:
                    logger.warning("Skipping evidence with invalid asset_type for question %s", q["id"])
                    continue

                resolved_origin = _resolve_evidence_origin(item, chunk_texts, chunk_by_id)
                if resolved_origin is None:
                    logger.warning("Skipping evidence with unresolved source attribution for question %s", q["id"])
                    continue

                source_id, chunk_id = resolved_origin

                evidence = EvidenceSpan(
                    id=uuid.uuid4(),
                    project_id=uuid.UUID(state["project_id"]),
                    source_id=uuid.UUID(source_id),
                    chunk_id=uuid.UUID(chunk_id) if chunk_id else None,
                    verbatim_quote=item["verbatim_quote"],
                    asset_type=normalized_asset_type,
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
                new_evidence_count += 1

                asset_type_upper = normalized_asset_type.upper()
                if asset_type_upper:
                    question_counts = question_found_counts.setdefault(q["id"], {})
                    question_counts[asset_type_upper] = question_counts.get(asset_type_upper, 0) + 1
                    new_asset_types_found.add(asset_type_upper)

            update_generation_progress(run_id, "phase3_research", "hunter", 20.0 + (i / len(questions)) * 10)

        await db.commit()

    research_gaps = []
    for q in questions:
        question_id = q["id"]
        required_assets_map = question_required_assets.get(question_id, {})
        expected_assets = sorted(question_expected_types.get(question_id, set()))
        found_counts = question_found_counts.get(question_id, {})
        found_assets = sorted(asset for asset, count in found_counts.items() if count > 0)
        required_assets = [
            {
                "type": asset_type,
                "min_count": required_assets_map[asset_type],
                "found_count": found_counts.get(asset_type, 0),
            }
            for asset_type in sorted(required_assets_map)
        ]
        missing_assets = sorted(
            asset_type
            for asset_type, min_count in required_assets_map.items()
            if found_counts.get(asset_type, 0) < min_count
        )
        if missing_assets:
            research_gaps.append({
                "question_id": question_id,
                "branch_id": q["branch_id"],
                "question": q["question"],
                "priority": q.get("priority", 5),
                "missing_assets": missing_assets,
                "required_assets": required_assets,
                "expected_assets": expected_assets,
                "found_assets": found_assets,
            })

    research_gaps.sort(key=lambda gap: (gap.get("priority", 5), gap.get("branch_id", ""), gap.get("question_id", "")))
    publish_event(run_id, evidence_found(new_evidence_count, sorted(new_asset_types_found)))
    publish_event(run_id, step_completed("phase3_research", "hunter_search"))
    publish_event(run_id, progress(30.0, f"Found {new_evidence_count} new evidence spans in corpus"))

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
    normalized: list[str] = []
    seen: set[str] = set()
    for item in asset_types:
        if not isinstance(item, str):
            continue
        asset_type = item.strip().upper()
        if not asset_type or asset_type not in _ALLOWED_EVIDENCE_TYPES:
            continue
        if asset_type in seen:
            continue
        seen.add(asset_type)
        normalized.append(asset_type)
    return normalized


def _normalize_single_asset_type(asset_type: object) -> str | None:
    if not isinstance(asset_type, str):
        return None
    normalized = asset_type.strip().upper()
    if not normalized or normalized not in _ALLOWED_EVIDENCE_TYPES:
        return None
    return normalized.lower()


def _normalize_required_targets(raw_required: list[dict] | None) -> list[dict[str, int | str]]:
    if not isinstance(raw_required, list):
        return []

    dedup: dict[str, int] = {}
    order: list[str] = []
    for item in raw_required:
        if not isinstance(item, dict):
            continue
        evidence_type_raw = item.get("type")
        min_count_raw = item.get("min_count")
        if not isinstance(evidence_type_raw, str):
            continue
        if isinstance(min_count_raw, bool) or not isinstance(min_count_raw, int) or min_count_raw < 1:
            continue
        evidence_type = evidence_type_raw.strip().upper()
        if not evidence_type or evidence_type not in _ALLOWED_EVIDENCE_TYPES:
            continue
        if evidence_type not in dedup:
            dedup[evidence_type] = min_count_raw
            order.append(evidence_type)
        else:
            dedup[evidence_type] = max(dedup[evidence_type], min_count_raw)

    return [{"type": evidence_type, "min_count": dedup[evidence_type]} for evidence_type in order]


def _extract_node_evidence_contract(node: dict) -> tuple[list[dict[str, int | str]], list[str]]:
    required_targets = _normalize_required_targets(node.get("required_evidence"))
    expected_types = _normalize_asset_types(node.get("expected_evidence"))

    if not required_targets:
        # Backward compatibility for old manifests.
        legacy_expected = _normalize_asset_types(node.get("expected_evidence_type"))
        required_targets = [{"type": asset_type, "min_count": 1} for asset_type in legacy_expected]

    required_types = {item["type"] for item in required_targets}
    expected_types = [asset_type for asset_type in expected_types if asset_type not in required_types]
    return required_targets, expected_types


def _merge_required_assets(
    branch_required_assets: dict[str, dict[str, int]],
    branch_id: str,
    required_targets: list[dict[str, int | str]],
) -> None:
    aggregate = branch_required_assets.setdefault(branch_id, {})
    for target in required_targets:
        asset_type = str(target.get("type", "")).upper()
        min_count = target.get("min_count")
        if not asset_type:
            continue
        if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
            continue
        aggregate[asset_type] = max(aggregate.get(asset_type, 0), min_count)


def _resolve_evidence_origin(
    evidence_item: dict,
    chunk_texts: list[dict],
    chunk_by_id: dict[str, dict],
) -> tuple[str, str | None] | None:
    chunk_id_raw = evidence_item.get("chunk_id")
    if isinstance(chunk_id_raw, str):
        direct_match = chunk_by_id.get(chunk_id_raw.strip())
        if direct_match and direct_match.get("source_id"):
            return str(direct_match["source_id"]), str(direct_match.get("chunk_id"))

    quote = evidence_item.get("verbatim_quote")
    if not isinstance(quote, str) or not quote.strip():
        return None

    matched_chunk = _find_chunk_for_quote(quote, chunk_texts)
    if matched_chunk is None or not matched_chunk.get("source_id"):
        return None
    return str(matched_chunk["source_id"]), str(matched_chunk.get("chunk_id"))


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _find_chunk_for_quote(quote: str, chunk_texts: list[dict]) -> dict | None:
    quote_raw = quote.strip()
    if not quote_raw:
        return None

    quote_normalized = _normalize_text(quote_raw)
    quote_prefix = quote_normalized[:120] if len(quote_normalized) >= 80 else quote_normalized

    for chunk in chunk_texts:
        content = str(chunk.get("content", ""))
        if not content:
            continue
        if quote_raw in content:
            return chunk

    for chunk in chunk_texts:
        content = str(chunk.get("content", ""))
        if not content:
            continue
        normalized_content = _normalize_text(content)
        if quote_normalized in normalized_content:
            return chunk
        if quote_prefix and quote_prefix in normalized_content:
            return chunk

    return None


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
