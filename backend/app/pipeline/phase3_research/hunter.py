"""Phase 3.1: Hunter — closed-book retrieval from project corpus."""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.phase3_research.normalization import normalize_research_text
from app.services.evidence_store import (
    EvidenceProvenanceContext,
    ensure_evidence_provenance,
    get_or_create_canonical_evidence,
    normalize_direct_or_contextual,
)
from app.pipeline.state import PipelineState
from app.pipeline.phase3_research.tools import vector_search_tool, keyword_search_tool, get_chunk_content
from app.pipeline.llm_json import parse_llm_json
from app.services.llm import call_haiku_structured
from app.observability import safe_set_run, trace_span
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, evidence_found, progress

logger = logging.getLogger(__name__)

RETRIEVAL_TOP_K = 40
RETRIEVAL_POOL_SIZE = 40
EXTRACTION_CHUNK_LIMIT = 8
DIVERSITY_SOURCE_PENALTY = 0.15
DIVERSITY_PARENT_PENALTY = 0.08
_REPO_ROOT = Path(__file__).resolve().parents[4]

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

_INTENT_RETRIEVAL_GOALS = {
    "history": "surface chronology, origin, and turning points rather than generic background",
    "explain": "surface direct mechanism and definitions rather than adjacent commentary",
    "compare": "surface explicit alternatives, differences, contrasts, and versus framing",
    "debate": "surface evaluative arguments, tradeoffs, criticism, and explicit pro/con positions",
    "case_study": "surface concrete episodes with actors, actions, consequences, and outcomes",
}

EXTRACT_EVIDENCE_SYSTEM = """You are an evidence extractor. Given chunks of text and a research question, extract relevant evidence spans.

Input may include:
- question_intent: why this question exists in the episode structure (history/explain/compare/debate/case_study).
- required_evidence: strict evidence types that must be found if present in the text.
- expected_evidence_types: useful but non-blocking evidence targets.
Prioritize required_evidence first, then expected evidence types.

RELEVANCE: Prefer evidence that DIRECTLY answers the research question. If a direct answer exists, do not extract peripheral context instead.

INTENT RULES:
- For debate questions, prefer explicit arguments, comparative advantages/disadvantages, criticisms, tradeoffs, and attributed pro/con positions over generic background.
- For compare questions, prefer explicit side-by-side differences, alternatives, and contrastive framing over adjacent description.
- For case_study questions, prefer concrete episodes with actors, actions, consequences, and outcomes over general thematic background.
- Do not flatten evaluative or comparative material into generic mechanism/history evidence when the question intent asks for argument, comparison, or case analysis.

VERBATIM RULE: verbatim_quote must be copied character-for-character from the source, including exact capitalisation and punctuation. Think of it as a text highlight: a continuous span from position A to position B with nothing changed, added, or removed. If two relevant sentences are not directly adjacent in the source, return each as a separate item — do not combine them into one quote.

Each input chunk has an "index" field. Return that index in chunk_index so the quote can be attributed to the correct source.

Return JSON array:
[
  {
    "chunk_index": 0,
    "verbatim_quote": "exact quote from the text",
    "asset_type": "STATISTIC|ANECDOTE|EXPERT_QUOTE|DEFINITION|MECHANISM|CASE|TIMELINE|CONTRAST",
    "direct_or_contextual": "direct|contextual",
    "narrative_value_score": 0.0-1.0,
    "tellability_notes": "why this is interesting for a podcast",
    "locator": {"type": "section", "value": "heading path"}
  }
]

Use direct_or_contextual="direct" when the quote explicitly answers the question as asked.
Use direct_or_contextual="contextual" for useful background, analogy, or adjacent framing that does not answer it directly.

Only extract genuinely relevant evidence. Return [] if nothing relevant found."""

KEYWORD_QUERY_SYSTEM = """Generate up to 3 short keyword retrieval queries for a closed-book corpus search.
Return JSON:
{"queries": ["..."]}

Rules:
- Prefer the language that best matches the sample corpus snippets.
- Use question_intent plus required/expected evidence to preserve the search shape.
- Use intent_retrieval_goal and intent_seed_queries to surface the right evidence shape:
  - debate -> arguments, tradeoffs, criticism, pro/con positions, reliability comparisons
  - compare -> explicit alternatives, versus framing, differences, side-by-side contrasts
  - case_study -> named episodes, incidents, interventions, actors, consequences, aftermath
- Keep queries short and lexical, suitable for PostgreSQL full-text search.
- Preserve named entities, dates, acronyms, and domain-specific terms.
- Output only JSON."""


async def hunt_closed_book(state: PipelineState) -> dict:
    """LangGraph node: search project corpus for evidence matching research tree."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase3_research", "Searching project corpus for evidence"))
    update_generation_progress(run_id, "phase3_research", "hunter", 20.0)

    source_ids = state.get("source_ids", [])
    if not source_ids:
        return {"evidence_ids": [], "current_phase": "phase3_research"}

    all_evidence_ids = list(state.get("evidence_ids", []))
    seen_evidence_ids = set(all_evidence_ids)
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
                "intent": branch.get("intent", ""),
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
                    "intent": child.get("intent", branch.get("intent", "")),
                    "priority": child.get("priority", branch.get("priority", 5)),
                    "required_targets": child_required,
                    "expected_types": child_expected,
                })

        publish_event(run_id, step_started("phase3_research", "hunter_search", f"Searching {len(questions)} questions across {len(source_ids)} sources"))

        for i, q in enumerate(questions):
            with trace_span(
                "research.hunter.question",
                run_type="chain",
                inputs={
                    "question_id": q["id"],
                    "branch_id": q["branch_id"],
                    "intent": q.get("intent"),
                    "priority": q.get("priority", 5),
                    "question_preview": _clip_text(q["question"]),
                    "required_evidence": q["required_targets"],
                    "expected_evidence": q["expected_types"],
                },
                tags=["research", "hunter", "hunter-question"],
            ) as question_run:
                saved_debug_items: list[dict] = []

                # Hybrid search: vector + keyword, RRF merge
                intent_seed_queries = _build_intent_seed_queries(q["question"], q.get("intent"))
                vector_queries = _merge_query_candidates(q["question"], intent_seed_queries)
                vector_results = await _run_vector_queries(vector_queries, source_ids, db, top_k=RETRIEVAL_TOP_K)
                keyword_queries = await _build_keyword_queries(
                    q["question"],
                    q.get("intent"),
                    q["required_targets"],
                    q["expected_types"],
                    vector_results,
                    intent_seed_queries=intent_seed_queries,
                )
                keyword_results = await _run_keyword_queries(keyword_queries, source_ids, db, top_k=RETRIEVAL_TOP_K)

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
                    safe_set_run(
                        question_run,
                        outputs={
                            "vector_hit_count": len(vector_results),
                            "keyword_hit_count": len(keyword_results),
                            "merged_hit_count": len(merged),
                            "selected_chunk_count": 0,
                            "saved_evidence_count": 0,
                        },
                    )
                    _dump_hunter_question_debug(
                        run_id=run_id,
                        question=q,
                        status="no_selected_chunks",
                        vector_results=vector_results,
                        keyword_results=keyword_results,
                        merged_results=merged,
                        selected_chunks=top_chunks,
                        loaded_chunks=[],
                        extracted_items=None,
                        saved_items=saved_debug_items,
                    )
                    continue

                # Load full content for top chunks
                chunk_texts = []
                for hit in top_chunks:
                    content = await get_chunk_content(hit["chunk_id"], db)
                    if content:
                        chunk_texts.append(content)

                if not chunk_texts:
                    safe_set_run(
                        question_run,
                        outputs={
                            "vector_hit_count": len(vector_results),
                            "keyword_hit_count": len(keyword_results),
                            "merged_hit_count": len(merged),
                            "selected_chunk_count": len(top_chunks),
                            "loaded_chunk_count": 0,
                            "saved_evidence_count": 0,
                        },
                    )
                    _dump_hunter_question_debug(
                        run_id=run_id,
                        question=q,
                        status="no_loaded_chunks",
                        vector_results=vector_results,
                        keyword_results=keyword_results,
                        merged_results=merged,
                        selected_chunks=top_chunks,
                        loaded_chunks=[],
                        extracted_items=None,
                        saved_items=saved_debug_items,
                    )
                    continue

                # Extract evidence using Haiku
                prompt = json.dumps({
                    "research_question": q["question"],
                    "question_intent": q.get("intent"),
                    "required_evidence": q["required_targets"],
                    "expected_evidence_types": q["expected_types"],
                    "chunks": [{"index": i, "content": c["content"], "source": c.get("source_title", ""), "heading": c.get("heading_path", [])} for i, c in enumerate(chunk_texts)],
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
                    safe_set_run(
                        question_run,
                        outputs={
                            "vector_hit_count": len(vector_results),
                            "keyword_hit_count": len(keyword_results),
                            "merged_hit_count": len(merged),
                            "selected_chunk_count": len(top_chunks),
                            "loaded_chunk_count": len(chunk_texts),
                            "extraction_failed": True,
                        },
                    )
                    _dump_hunter_question_debug(
                        run_id=run_id,
                        question=q,
                        status="extraction_failed",
                        vector_results=vector_results,
                        keyword_results=keyword_results,
                        merged_results=merged,
                        selected_chunks=top_chunks,
                        loaded_chunks=chunk_texts,
                        extracted_items=None,
                        saved_items=saved_debug_items,
                    )
                    continue

                if not isinstance(evidence_items, list):
                    safe_set_run(
                        question_run,
                        outputs={
                            "vector_hit_count": len(vector_results),
                            "keyword_hit_count": len(keyword_results),
                            "merged_hit_count": len(merged),
                            "selected_chunk_count": len(top_chunks),
                            "loaded_chunk_count": len(chunk_texts),
                            "extracted_item_count": 0,
                            "saved_evidence_count": 0,
                        },
                    )
                    _dump_hunter_question_debug(
                        run_id=run_id,
                        question=q,
                        status="invalid_extractor_payload",
                        vector_results=vector_results,
                        keyword_results=keyword_results,
                        merged_results=merged,
                        selected_chunks=top_chunks,
                        loaded_chunks=chunk_texts,
                        extracted_items=evidence_items,
                        saved_items=saved_debug_items,
                    )
                    continue

                saved_for_question = 0
                invalid_type_count = 0
                unresolved_origin_count = 0

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
                        invalid_type_count += 1
                        logger.warning("Skipping evidence with invalid asset_type for question %s", q["id"])
                        continue

                    resolved_origin = _resolve_evidence_origin(item, chunk_texts, chunk_by_id)
                    if resolved_origin is None:
                        unresolved_origin_count += 1
                        logger.warning("Skipping evidence with unresolved source attribution for question %s", q["id"])
                        continue

                    source_id, chunk_id = resolved_origin

                    evidence, created = await get_or_create_canonical_evidence(
                        db=db,
                        project_id=state["project_id"],
                        source_id=source_id,
                        chunk_id=chunk_id,
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
                    await ensure_evidence_provenance(
                        db=db,
                        evidence_id=evidence.id,
                        context=EvidenceProvenanceContext(
                            generation_run_id=state["generation_run_id"],
                            manifest_id=state.get("manifest_id"),
                            question_id=q["id"],
                            branch_id=q["branch_id"],
                            asset_type=normalized_asset_type,
                            retrieved_by="hunter",
                            query_text=q["question"],
                            direct_or_contextual=normalize_direct_or_contextual(item.get("direct_or_contextual")),
                        ),
                    )
                    evidence_id = str(evidence.id)
                    if evidence_id not in seen_evidence_ids:
                        seen_evidence_ids.add(evidence_id)
                        all_evidence_ids.append(evidence_id)
                    if created:
                        new_evidence_count += 1
                    saved_for_question += 1
                    source_chunk = chunk_by_id.get(chunk_id) if chunk_id else None
                    saved_debug_items.append(
                        {
                            "evidence_id": evidence_id,
                            "asset_type": normalized_asset_type,
                            "source_id": source_id,
                            "chunk_id": chunk_id,
                            "canonical_reused": not created,
                            "source_title": source_chunk.get("source_title") if isinstance(source_chunk, dict) else None,
                            "heading_path": source_chunk.get("heading_path") if isinstance(source_chunk, dict) else None,
                            "verbatim_quote": item["verbatim_quote"],
                            "quote_preview": _clip_text(item["verbatim_quote"], limit=220),
                            "locator": item.get("locator"),
                            "direct_or_contextual": normalize_direct_or_contextual(item.get("direct_or_contextual")),
                            "narrative_value_score": item.get("narrative_value_score"),
                            "tellability_notes": item.get("tellability_notes"),
                        }
                    )

                    asset_type_upper = normalized_asset_type.upper()
                    if asset_type_upper:
                        question_counts = question_found_counts.setdefault(q["id"], {})
                        question_counts[asset_type_upper] = question_counts.get(asset_type_upper, 0) + 1
                        new_asset_types_found.add(asset_type_upper)

                safe_set_run(
                    question_run,
                    outputs={
                        "vector_hit_count": len(vector_results),
                        "keyword_hit_count": len(keyword_results),
                        "merged_hit_count": len(merged),
                        "selected_chunk_count": len(top_chunks),
                        "loaded_chunk_count": len(chunk_texts),
                        "extracted_item_count": len(evidence_items),
                        "saved_evidence_count": saved_for_question,
                        "invalid_asset_type_count": invalid_type_count,
                        "unresolved_origin_count": unresolved_origin_count,
                        "selected_chunks_sample": [hit.get("chunk_id") for hit in top_chunks[:5]],
                    },
                )
                _dump_hunter_question_debug(
                    run_id=run_id,
                    question=q,
                    status="ok",
                    vector_results=vector_results,
                    keyword_results=keyword_results,
                    merged_results=merged,
                    selected_chunks=top_chunks,
                    loaded_chunks=chunk_texts,
                    extracted_items=evidence_items,
                    saved_items=saved_debug_items,
                    invalid_asset_type_count=invalid_type_count,
                    unresolved_origin_count=unresolved_origin_count,
                )

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
                "intent": q.get("intent"),
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


def _safe_filename_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    token = token.strip("-_")
    return token or "unknown"


def _resolve_dump_dir(raw_dir: str) -> Path:
    path = Path(raw_dir).expanduser()
    if path.is_absolute():
        return path
    return _REPO_ROOT / path


def _summarize_hits(hits: list[dict], limit: int = 10) -> list[dict]:
    summarized: list[dict] = []
    for hit in hits[:limit]:
        summarized.append(
            {
                "chunk_id": hit.get("chunk_id"),
                "source_id": hit.get("source_id"),
                "parent_chunk_id": hit.get("parent_chunk_id"),
                "score": hit.get("score"),
                "rrf_score": hit.get("rrf_score"),
            }
        )
    return summarized


def _summarize_loaded_chunks(chunks: list[dict], limit: int = 8) -> list[dict]:
    summarized: list[dict] = []
    for chunk in chunks[:limit]:
        content = str(chunk.get("content", ""))
        summarized.append(
            {
                "chunk_id": chunk.get("chunk_id"),
                "source_id": chunk.get("source_id"),
                "source_title": chunk.get("source_title"),
                "heading_path": chunk.get("heading_path") or [],
                "content_chars": len(content),
                "content_preview": _clip_text(content, limit=300),
                "content_full": content,
            }
        )
    return summarized


def _summarize_extracted_items(items: object, limit: int | None = None) -> list[dict]:
    if not isinstance(items, list):
        return []

    iterable = items if limit is None else items[:limit]
    summarized: list[dict] = []
    for item in iterable:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("verbatim_quote", ""))
        summarized.append(
            {
                "chunk_index": item.get("chunk_index"),
                "asset_type": item.get("asset_type"),
                "direct_or_contextual": item.get("direct_or_contextual"),
                "verbatim_quote": quote,
                "quote_preview": _clip_text(quote, limit=220),
                "locator": item.get("locator"),
                "narrative_value_score": item.get("narrative_value_score"),
                "tellability_notes": item.get("tellability_notes"),
                "chunk_id": item.get("chunk_id"),
            }
        )
    return summarized


def _dump_hunter_question_debug(
    *,
    run_id: str,
    question: dict,
    status: str,
    vector_results: list[dict],
    keyword_results: list[dict],
    merged_results: list[dict],
    selected_chunks: list[dict],
    loaded_chunks: list[dict],
    extracted_items: object,
    saved_items: list[dict],
    invalid_asset_type_count: int = 0,
    unresolved_origin_count: int = 0,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return

    try:
        target_dir = _resolve_dump_dir(dump_dir) / "hunter"
        target_dir.mkdir(parents=True, exist_ok=True)

        question_id = str(question.get("id", "question"))
        question_text = str(question.get("question", ""))
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        q_hash = hashlib.sha1(question_text.encode("utf-8")).hexdigest()[:10]
        filename = (
            f"{ts}_run-{_safe_filename_token(run_id)}_"
            f"{_safe_filename_token(question_id)}_{q_hash}.json"
        )

        payload = {
            "run_id": run_id,
            "status": status,
            "question": {
                "id": question_id,
                "branch_id": question.get("branch_id"),
                "intent": question.get("intent"),
                "priority": question.get("priority"),
                "text": question_text,
                "required_evidence": question.get("required_targets", []),
                "expected_evidence": question.get("expected_types", []),
            },
            "retrieval": {
                "vector_hit_count": len(vector_results),
                "keyword_hit_count": len(keyword_results),
                "merged_hit_count": len(merged_results),
                "selected_chunk_count": len(selected_chunks),
                "loaded_chunk_count": len(loaded_chunks),
                "vector_top_hits": _summarize_hits(vector_results),
                "keyword_top_hits": _summarize_hits(keyword_results),
                "selected_chunks": _summarize_hits(selected_chunks, limit=12),
                "loaded_chunks": _summarize_loaded_chunks(loaded_chunks),
            },
            "extraction": {
                "extracted_item_count": len(extracted_items) if isinstance(extracted_items, list) else None,
                "extracted_items": _summarize_extracted_items(extracted_items),
                "extracted_items_sample": _summarize_extracted_items(extracted_items, limit=12),
                "saved_evidence_count": len(saved_items),
                "saved_evidence": saved_items[:12],
                "invalid_asset_type_count": invalid_asset_type_count,
                "unresolved_origin_count": unresolved_origin_count,
            },
        }

        (target_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.warning(
            "Failed to write hunter debug dump (run_id=%s, question_id=%s, status=%s)",
            run_id,
            question.get("id"),
            status,
            exc_info=True,
        )


def _clip_text(value: str, limit: int = 220) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


async def _build_keyword_queries(
    question: str,
    question_intent: str | None,
    required_targets: list[dict[str, int | str]],
    expected_types: list[str],
    vector_results: list[dict],
    *,
    intent_seed_queries: list[str] | None = None,
) -> list[str]:
    sample_chunks = [
        {
            "preview": item.get("preview", "")[:300],
            "heading_path": item.get("heading_path") or [],
        }
        for item in vector_results[:3]
        if isinstance(item, dict)
    ]
    if not sample_chunks:
        return _merge_query_candidates(question, intent_seed_queries or [])

    try:
        raw = await call_haiku_structured(
            system=KEYWORD_QUERY_SYSTEM,
            prompt=json.dumps(
                {
                    "question": question,
                    "question_intent": question_intent,
                    "intent_retrieval_goal": _intent_retrieval_goal(question_intent),
                    "intent_seed_queries": intent_seed_queries or [],
                    "required_evidence": required_targets,
                    "expected_evidence": expected_types,
                    "sample_chunks": sample_chunks,
                },
                ensure_ascii=False,
            ),
            max_tokens=256,
            temperature=0,
        )
        parsed = parse_llm_json(raw)
    except Exception:
        logger.warning("Keyword query rewrite failed", exc_info=True)
        return [question]

    raw_queries = parsed.get("queries", []) if isinstance(parsed, dict) else parsed
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in _merge_query_candidates(
        question,
        [
            *(intent_seed_queries or []),
            *(raw_queries if isinstance(raw_queries, list) else []),
        ],
    ):
        if not isinstance(candidate, str):
            continue
        cleaned = normalize_research_text(candidate).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped or [question]


async def _run_vector_queries(
    queries: list[str],
    source_ids: list[str],
    db,
    *,
    top_k: int,
) -> list[dict]:
    merged: dict[str, dict] = {}
    for query in queries:
        for hit in await vector_search_tool(query, top_k=top_k, source_ids=source_ids, db=db):
            chunk_id = hit.get("chunk_id")
            if not isinstance(chunk_id, str):
                continue
            current = merged.get(chunk_id)
            if current is None or float(hit.get("score", 0.0)) > float(current.get("score", 0.0)):
                merged[chunk_id] = hit
    return sorted(merged.values(), key=lambda item: float(item.get("score", 0.0)), reverse=True)


def _intent_retrieval_goal(question_intent: str | None) -> str:
    return _INTENT_RETRIEVAL_GOALS.get((question_intent or "").strip().lower(), "")


def _build_intent_seed_queries(question: str, question_intent: str | None) -> list[str]:
    base_question = normalize_research_text(question).strip()
    if not base_question:
        return []

    intent = (question_intent or "").strip().lower()
    variants: list[str]
    if intent == "debate":
        variants = [
            f"{base_question} arguments tradeoffs criticism reliability versus monopoly",
            f"{base_question} supporters opponents comparison advantages disadvantages",
        ]
    elif intent == "compare":
        variants = [
            f"{base_question} versus difference compared with alternative contrast",
            f"{base_question} comparison advantages disadvantages alternative system",
        ]
    elif intent == "case_study":
        variants = [
            f"{base_question} case study episode incident intervention consequences",
            f"{base_question} named example actors outcome aftermath",
        ]
    else:
        variants = []

    return _merge_query_candidates("", variants)


def _merge_query_candidates(base_query: str, candidates: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in [base_query, *candidates]:
        if not isinstance(candidate, str):
            continue
        cleaned = normalize_research_text(candidate).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


async def _run_keyword_queries(
    queries: list[str],
    source_ids: list[str],
    db,
    *,
    top_k: int,
) -> list[dict]:
    merged: dict[str, dict] = {}
    for query in queries:
        for hit in await keyword_search_tool(query, source_ids, db, top_k=top_k):
            chunk_id = hit.get("chunk_id")
            if not isinstance(chunk_id, str):
                continue
            current = merged.get(chunk_id)
            if current is None or float(hit.get("score", 0.0)) > float(current.get("score", 0.0)):
                merged[chunk_id] = hit
    return sorted(merged.values(), key=lambda item: float(item.get("score", 0.0)), reverse=True)


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


def _quote_in_chunk_strict(quote: str, chunk: dict) -> bool:
    """Full-match only check (no prefix shortcut) used to validate chunk_index."""
    content = str(chunk.get("content", ""))
    if not content:
        return False
    q = _normalize_text(quote)
    c = _normalize_text(content)
    return q in c or q.lower() in c.lower()


def _resolve_evidence_origin(
    evidence_item: dict,
    chunk_texts: list[dict],
    chunk_by_id: dict[str, dict],
) -> tuple[str, str | None] | None:
    quote = evidence_item.get("verbatim_quote")
    quote_str = quote.strip() if isinstance(quote, str) else ""

    # Primary path: LLM-provided chunk_index, validated with strict full-match.
    # Prefix fallback is intentionally excluded here — a partial match must not
    # prevent the global search from finding the chunk with the complete quote.
    chunk_index = evidence_item.get("chunk_index")
    if isinstance(chunk_index, int) and 0 <= chunk_index < len(chunk_texts):
        candidate = chunk_texts[chunk_index]
        if candidate.get("source_id"):
            if not quote_str or _quote_in_chunk_strict(quote_str, candidate):
                return str(candidate["source_id"]), str(candidate.get("chunk_id"))

    # Fallback: substring search across all chunks (handles wrong/missing chunk_index).
    if not quote_str:
        return None
    matched_chunk = _find_chunk_for_quote(quote_str, chunk_texts)
    if matched_chunk is None or not matched_chunk.get("source_id"):
        return None
    return str(matched_chunk["source_id"]), str(matched_chunk.get("chunk_id"))


def _normalize_text(value: str) -> str:
    return normalize_research_text(value)


def _find_chunk_for_quote(quote: str, chunk_texts: list[dict]) -> dict | None:
    quote_raw = quote.strip()
    if not quote_raw:
        return None

    quote_normalized = _normalize_text(quote_raw)

    # Pass 1: exact match (case-sensitive, normalized whitespace/escaping)
    for chunk in chunk_texts:
        content = str(chunk.get("content", ""))
        if not content:
            continue
        if quote_normalized in _normalize_text(content):
            return chunk

    # Pass 2: case-insensitive fallback (handles LLM capitalisation variations)
    quote_lower = quote_normalized.lower()
    quote_prefix_lower = quote_lower[:120] if len(quote_lower) >= 80 else quote_lower
    for chunk in chunk_texts:
        content = str(chunk.get("content", ""))
        if not content:
            continue
        content_lower = _normalize_text(content).lower()
        if quote_lower in content_lower:
            return chunk
        if quote_prefix_lower and quote_prefix_lower in content_lower:
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
