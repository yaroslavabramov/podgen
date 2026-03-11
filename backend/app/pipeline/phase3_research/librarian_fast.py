"""Phase 3.2A: Librarian Fast — open-web evidence search (non-blocking)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from urllib.parse import urlparse

from sqlalchemy import func, select

from app.config import settings
from app.ingestion.source_metadata import canonical_url, classify_authority_heuristic, classify_domain
from app.ingestion.web_quality import contains_blocking_signal
from app.models.chunk import Chunk
from app.models.episode_manifest import EpisodeManifest
from app.models.source import Source, SourceType
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.phase3_research.librarian_deep import trigger_deep_ingestion
from app.pipeline.phase3_research.tools import create_web_evidence, fetch_page_tool, web_search_tool
from app.pipeline.research_context import build_question_context_map
from app.pipeline.state import PipelineState
from app.pipeline.llm_json import parse_llm_json
from app.realtime.events import evidence_found, progress, step_completed, step_started
from app.realtime.publisher import publish_event
from app.services.llm import call_haiku_structured, call_sonnet_structured
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

EXTRACT_WEB_EVIDENCE_SYSTEM = """You are a web evidence extractor for a podcast research pipeline.
Given web pages, a research question, extractor mode, gap context, and curated excerpt windows, identify the most valuable evidence.

Return JSON array:
[
  {
    "url": "source URL",
    "verbatim_quote": "exact quote from the provided excerpt windows",
    "asset_type": "STATISTIC|ANECDOTE|EXPERT_QUOTE|DEFINITION|MECHANISM|CASE|TIMELINE|CONTRAST",
    "direct_or_contextual": "direct|contextual",
    "narrative_value_score": 0.0-1.0,
    "tellability_notes": "why this evidence is valuable for a podcast"
  }
]

Rules:
- Use only the provided excerpt windows, not outside assumptions.
- If extractor_mode="question_gap_fill", prioritize evidence that directly helps answer the research question and fills target_missing_assets when they are present.
- If extractor_mode="claim_corroboration", prioritize evidence that directly supports one of the claim_contexts / claim_statement.
- In claim_corroboration mode, treat question_background_assets as secondary context only; do not choose evidence merely because it matches broad question-level asset hints.
- Use direct_or_contextual="direct" when the quote directly answers the question as asked.
- Use direct_or_contextual="contextual" for background, analogy, or adjacent framing.
- For verifier gaps, prioritize independent corroboration and stronger source families over generic explainers.
- If existing_support_summary says support is contextual-only or single-source, prefer direct quotes from a different source family.
- Max 3 evidence items per question. Only extract genuinely valuable evidence.
- Return raw JSON only."""

FAST_WEB_QUERY_SYNTHESIS_SYSTEM = """You generate concise web search queries for open-web research.

Return JSON:
{
  "queries": ["query 1", "query 2", "query 3"]
}

Rules:
- Return 2-4 concise search-oriented queries.
- Prefer English queries for global open-web retrieval unless exact non-English wording is essential.
- Preserve named entities, acronyms, currencies, dates, law names, and exact numbers.
- Do not copy the full research question, full claim, or full search objective verbatim.
- Do not mechanically expand queries with generic scaffolding such as "definition", "mechanism", "how it works", "find a source that confirms", unless that phrase is part of a named concept.
- For question_gap_fill mode, queries should help answer the research question directly.
- For claim_corroboration mode, queries should center on claim anchors and direct corroboration, not broad thematic wording.
- When needs_source_diversification is true, include at least one authoritative / institutional / archival query variant.
- Return JSON only."""
_FAST_WEB_MAX_RESULTS_PER_QUERY = 5
_FAST_WEB_INITIAL_PAGE_LIMIT = 2
_FAST_WEB_VERIFIER_PAGE_LIMIT = 3
_FAST_WEB_EXCERPT_MAX_WINDOWS = 4
_FAST_WEB_MIN_PREVIEW_REUSE_CHARS = 280
_FAST_WEB_MIN_PAGE_CHARS = 220
_FAST_WEB_LOW_VALUE_OVERLAP_MIN = 0.04
_FAST_WEB_WINDOW_MIN_CHARS = 800
_FAST_WEB_WINDOW_MAX_CHARS = 1600
_FAST_WEB_WINDOW_FALLBACK_CHARS = 1400
_FAST_WEB_MAX_QUERY_CHARS = 320
_FAST_WEB_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "that", "this", "is", "was", "were",
    "with", "from", "into", "about", "what", "when", "where", "why", "how", "which", "who", "does", "did",
    "это", "как", "что", "когда", "почему", "для", "при", "под", "или", "и", "в", "на",
}


async def research_fast_web(state: PipelineState) -> dict:
    """LangGraph node: fast web research for evidence gaps."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase3_research", "fast_web", "Searching open web for additional evidence"))

    all_evidence_ids = list(dict.fromkeys(state.get("evidence_ids", [])))
    iteration_counts = dict(state.get("iteration_counts", {}))
    research_gaps = state.get("research_gaps", []) or []
    mode = _determine_fast_web_mode(research_gaps)
    processed_gap_count = _current_gap_count(iteration_counts, mode)
    gap_budget = _gap_budget_for_mode(mode)

    if processed_gap_count >= gap_budget:
        logger.info("Fast web governor hit for mode %s: %d gaps processed", mode, processed_gap_count)
        return {"evidence_ids": all_evidence_ids, "iteration_counts": iteration_counts}

    debug_attempts: list[dict] = []
    selected_gap_payloads: list[dict] = []

    async with async_session_factory() as db:
        if not state.get("manifest_id"):
            return {"evidence_ids": all_evidence_ids}

        result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
        )
        manifest = result.scalar_one_or_none()
        if not manifest or not manifest.research_tree_json:
            return {"evidence_ids": all_evidence_ids}

        tree = manifest.research_tree_json
        question_context_map = build_question_context_map(tree)
        branches = tree.get("branches", [])

        gap_branches = _select_gap_branches(
            branches,
            research_gaps,
            question_context_map=question_context_map,
        )
        if not gap_branches:
            logger.info("No research gaps detected for fast web step")
            return {
                "evidence_ids": all_evidence_ids,
                "iteration_counts": iteration_counts,
                "research_gaps": research_gaps,
                "current_phase": "phase3_research",
            }

        known_web_sources = await _load_known_web_source_index(db, project_id=state["project_id"])
        selected_gap_payloads = [
            {
                "question_id": gap.get("question_id", ""),
                "branch_id": gap.get("branch_id", ""),
                "gap_origin": gap.get("gap_origin", "hunter"),
                "question": gap.get("question", ""),
                "intent": gap.get("intent", ""),
                "support_strength": gap.get("support_strength", ""),
            }
            for gap in gap_branches
        ]

        new_evidence_count = 0
        used_source_families: dict[str, int] = {}
        for gap in gap_branches:
            if processed_gap_count >= gap_budget:
                break
            processed_gap_count += 1

            question = gap["question"]
            extractor_mode = _extractor_mode_for_gap(gap)
            target_missing_assets = _normalize_asset_types(gap.get("missing_assets", []))
            queries = await _build_search_queries(
                question,
                question_intent=gap.get("intent", ""),
                missing_assets=target_missing_assets if extractor_mode == "question_gap_fill" else [],
                required_assets=gap.get("required_assets", []),
                expected_assets=gap.get("expected_assets", []),
                podcast_language=state.get("language", ""),
                mode=mode,
                gap_origin=str(gap.get("gap_origin", "hunter")),
                claim_statement=str(gap.get("claim_statement", "")),
                search_objective=str(gap.get("search_objective", "")),
                existing_support_summary=str(gap.get("existing_support_summary", "")),
                claim_contexts=gap.get("claim_contexts", []),
            )

            gap_debug: dict = {
                "question_id": gap.get("question_id", ""),
                "branch_id": gap.get("branch_id", ""),
                "gap_origin": gap.get("gap_origin", "hunter"),
                "question": question,
                "intent": gap.get("intent", ""),
                "missing_assets": target_missing_assets,
                "required_assets": gap.get("required_assets", []),
                "expected_assets": gap.get("expected_assets", []),
                "support_strength": gap.get("support_strength", ""),
                "search_objective": gap.get("search_objective", ""),
                "queries": queries,
                "mode": mode,
            }

            try:
                search_results, search_failures = await _run_search_queries(queries)
                ranked_results = _rerank_search_results(
                    search_results,
                    gap=gap,
                    known_web_sources=known_web_sources,
                    used_source_families=used_source_families,
                )
                gap_debug["search_results"] = ranked_results
                gap_debug["search_failures"] = search_failures

                page_contents, page_handling = await _prepare_gap_pages(
                    gap=gap,
                    ranked_results=ranked_results,
                    known_web_sources=known_web_sources,
                )
                gap_debug["page_handling"] = page_handling

                if not page_contents:
                    gap_debug["status"] = "no_page_candidates"
                    debug_attempts.append(gap_debug)
                    _dump_fast_web_debug(
                        run_id=run_id,
                        dump_type=f"gap-{gap.get('branch_id', 'unknown')}-{gap.get('question_id', 'unknown')}",
                        payload=gap_debug,
                    )
                    continue

                prompt_payload = {
                    "extractor_mode": extractor_mode,
                    "research_question": question,
                    "question_intent": gap.get("intent"),
                    "gap_origin": gap.get("gap_origin", "hunter"),
                    "claim_statement": gap.get("claim_statement", ""),
                    "search_objective": gap.get("search_objective", ""),
                    "existing_support_summary": gap.get("existing_support_summary", ""),
                    "claim_contexts": gap.get("claim_contexts", []),
                    "required_evidence": gap.get("required_assets", []) if extractor_mode == "question_gap_fill" else [],
                    "expected_evidence": gap.get("expected_assets", []) if extractor_mode == "question_gap_fill" else [],
                    "target_missing_assets": target_missing_assets if extractor_mode == "question_gap_fill" else [],
                    "question_background_assets": {
                        "required_evidence": gap.get("required_assets", []),
                        "expected_evidence": gap.get("expected_assets", []),
                        "target_missing_assets": target_missing_assets,
                    } if extractor_mode == "claim_corroboration" else {},
                    "web_pages": page_contents,
                }
                gap_debug["prompt_payload"] = prompt_payload

                raw = await call_sonnet_structured(
                    system=EXTRACT_WEB_EVIDENCE_SYSTEM,
                    prompt=json.dumps(prompt_payload, ensure_ascii=False),
                    max_tokens=3072,
                    temperature=0.2,
                )
                evidence_items = parse_llm_json(raw)
                gap_debug["llm_output"] = {
                    "raw": raw,
                    "parsed": evidence_items,
                    "parsed_type": type(evidence_items).__name__ if evidence_items is not None else None,
                }
                if not isinstance(evidence_items, list):
                    gap_debug["status"] = "invalid_llm_output"
                    debug_attempts.append(gap_debug)
                    _dump_fast_web_debug(
                        run_id=run_id,
                        dump_type=f"gap-{gap.get('branch_id', 'unknown')}-{gap.get('question_id', 'unknown')}",
                        payload=gap_debug,
                    )
                    continue

                accepted_items: list[dict] = []
                rejected_items: list[dict] = []
                for item in evidence_items:
                    if not isinstance(item, dict):
                        rejected_items.append({"reason": "non_dict_item", "item": item})
                        continue
                    if not item.get("verbatim_quote") or not item.get("url"):
                        rejected_items.append({"reason": "missing_quote_or_url", "item": item})
                        continue

                    normalized_asset_types = _normalize_asset_types([item.get("asset_type")])
                    if not normalized_asset_types:
                        rejected_items.append({"reason": "invalid_asset_type", "item": item})
                        continue
                    asset_type = normalized_asset_types[0]
                    if target_missing_assets and asset_type not in target_missing_assets:
                        rejected_items.append({"reason": "asset_type_not_targeted", "item": item})
                        continue

                    source_page = next(
                        (page for page in page_contents if page.get("url") == item["url"]),
                        None,
                    )
                    eid = await create_web_evidence(
                        url=item["url"],
                        quote=item["verbatim_quote"],
                        asset_type=asset_type.lower(),
                        project_id=state["project_id"],
                        db=db,
                        generation_run_id=state["generation_run_id"],
                        manifest_id=state.get("manifest_id"),
                        question_id=gap.get("question_id", ""),
                        branch_id=gap.get("branch_id", ""),
                        query_text=str(source_page.get("query_text", question)) if isinstance(source_page, dict) else question,
                        retrieval_score=item.get("narrative_value_score"),
                        direct_or_contextual=item.get("direct_or_contextual"),
                        title=str(source_page.get("title", "")) if isinstance(source_page, dict) else None,
                    )
                    all_evidence_ids.append(eid)
                    new_evidence_count += 1
                    accepted_items.append(
                        {
                            "evidence_id": eid,
                            "asset_type": asset_type,
                            "url": item["url"],
                            "direct_or_contextual": item.get("direct_or_contextual"),
                            "quote_preview": _clip_text(str(item.get("verbatim_quote", "")), 220),
                        }
                    )

                gap_debug["accepted_items"] = accepted_items
                gap_debug["rejected_items"] = rejected_items
                gap_debug["status"] = "ok"
                for source_url in (
                    [str(item.get("url", "")).strip() for item in accepted_items]
                    or [str(page.get("url", "")).strip() for page in page_contents]
                ):
                    family = _source_family(source_url)
                    if family:
                        used_source_families[family] = used_source_families.get(family, 0) + 1
                debug_attempts.append(gap_debug)
                _dump_fast_web_debug(
                    run_id=run_id,
                    dump_type=f"gap-{gap.get('branch_id', 'unknown')}-{gap.get('question_id', 'unknown')}",
                    payload=gap_debug,
                )
            except Exception:
                logger.warning("Fast web search failed for question: %s", question, exc_info=True)
                gap_debug["status"] = "exception"
                gap_debug["error"] = "fast_web_gap_failed"
                debug_attempts.append(gap_debug)
                _dump_fast_web_debug(
                    run_id=run_id,
                    dump_type=f"gap-{gap.get('branch_id', 'unknown')}-{gap.get('question_id', 'unknown')}",
                    payload=gap_debug,
                )
                continue

        await db.commit()

    from app.models.source import IngestionStatus

    async with async_session_factory() as db2:
        active_result = await db2.execute(
            select(func.count(Source.id)).where(
                Source.generation_run_id == uuid.UUID(state["generation_run_id"]),
                Source.is_temporary.is_(True),
                Source.ingestion_status.in_([IngestionStatus.ENQUEUED, IngestionStatus.PROCESSING]),
            )
        )
        deep_active = active_result.scalar() or 0

        if deep_active < settings.max_deep_ingestion_concurrent:
            deep_result = await db2.execute(
                select(Source).where(
                    Source.project_id == uuid.UUID(state["project_id"]),
                    Source.is_temporary.is_(True),
                    Source.fully_ingested.is_(False),
                    Source.is_duplicate_of.is_(None),
                    Source.generation_run_id == uuid.UUID(state["generation_run_id"]),
                    Source.ingestion_status.in_([IngestionStatus.PENDING, IngestionStatus.COMPLETED]),
                )
            )
            deep_candidates = deep_result.scalars().all()

            for src in deep_candidates:
                if deep_active >= settings.max_deep_ingestion_concurrent:
                    break
                try:
                    trigger_deep_ingestion(str(src.id), run_id)
                    src.ingestion_status = IngestionStatus.ENQUEUED
                    deep_active += 1
                except Exception:
                    logger.warning("Failed to trigger deep ingestion for source %s", src.id)

            await db2.commit()

    _store_fast_web_iteration_counts(iteration_counts, mode=mode, processed_gap_count=processed_gap_count)
    iteration_counts["deep_ingestion_active"] = deep_active

    publish_event(run_id, step_completed("phase3_research", "fast_web"))
    if new_evidence_count > 0:
        publish_event(run_id, evidence_found(new_evidence_count, ["web"]))
        publish_event(run_id, progress(33.0, f"Found {new_evidence_count} additional web evidence"))

    _dump_fast_web_debug(
        run_id=run_id,
        dump_type="summary",
        payload={
            "run_id": run_id,
            "mode": mode,
            "gap_budget": gap_budget,
            "processed_gap_count": processed_gap_count,
            "selected_gaps": selected_gap_payloads,
            "new_evidence_count": new_evidence_count,
            "deep_ingestion_active": deep_active,
            "iteration_counts": iteration_counts,
            "attempt_count": len(debug_attempts),
        },
    )

    return {
        "evidence_ids": list(dict.fromkeys(all_evidence_ids)),
        "iteration_counts": iteration_counts,
        "research_gaps": research_gaps,
        "current_phase": "phase3_research",
    }


def _determine_fast_web_mode(research_gaps: list[dict] | None) -> str:
    for gap in research_gaps or []:
        if isinstance(gap, dict) and str(gap.get("gap_origin", "")).strip().lower() == "verifier":
            return "verifier_corroboration"
    return "hunter_gap_fill"


def _extractor_mode_for_gap(gap: dict) -> str:
    if str(gap.get("gap_origin", "")).strip().lower() == "verifier":
        return "claim_corroboration"
    return "question_gap_fill"


def _fast_web_counter_key(mode: str) -> str:
    return "fast_web_verifier_gap_count" if mode == "verifier_corroboration" else "fast_web_initial_gap_count"


def _gap_budget_for_mode(mode: str) -> int:
    if mode == "verifier_corroboration":
        return settings.max_fast_web_verifier_gaps
    return settings.max_fast_web_initial_gaps


def _current_gap_count(iteration_counts: dict, mode: str) -> int:
    counter_key = _fast_web_counter_key(mode)
    if counter_key in iteration_counts:
        return int(iteration_counts.get(counter_key, 0) or 0)
    if mode == "hunter_gap_fill":
        return int(iteration_counts.get("fast_web_gap_count", iteration_counts.get("fast_web_per_act", 0)) or 0)
    return 0


def _store_fast_web_iteration_counts(iteration_counts: dict, *, mode: str, processed_gap_count: int) -> None:
    counter_key = _fast_web_counter_key(mode)
    iteration_counts[counter_key] = processed_gap_count
    total_count = int(iteration_counts.get("fast_web_initial_gap_count", 0) or 0) + int(
        iteration_counts.get("fast_web_verifier_gap_count", 0) or 0
    )
    iteration_counts["fast_web_gap_count"] = total_count
    iteration_counts["fast_web_per_act"] = total_count
    iteration_counts["fast_web_mode"] = mode


def _normalize_required_assets(required_assets: list[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    for item in required_assets or []:
        if not isinstance(item, dict):
            continue
        asset_type = str(item.get("type", "")).strip().upper()
        if not asset_type:
            continue
        try:
            min_count = int(item.get("min_count", 1) or 1)
        except Exception:
            min_count = 1
        normalized.append({"type": asset_type, "min_count": max(min_count, 1)})
    return normalized


def _select_gap_branches(
    branches: list[dict],
    research_gaps: list[dict] | None,
    *,
    question_context_map: dict[str, dict] | None = None,
) -> list[dict]:
    """Prefer explicit gaps; fallback to priority heuristic for compatibility."""
    question_context_map = question_context_map or {}
    if isinstance(research_gaps, list):
        if not research_gaps:
            return []

        normalized = []
        for gap in research_gaps:
            if not isinstance(gap, dict):
                continue
            question_id = str(gap.get("question_id", "")).strip()
            branch_id = str(gap.get("branch_id", "")).strip()
            question_context = question_context_map.get(question_id, {})
            question = str(gap.get("question") or question_context.get("question") or "").strip()
            if not question:
                continue

            context_required = _normalize_required_assets(question_context.get("required_evidence", []))
            context_expected = _normalize_asset_types(question_context.get("expected_evidence", []))
            gap_required = _normalize_required_assets(gap.get("required_assets", []))
            gap_expected = _normalize_asset_types(gap.get("expected_assets", []))
            found_assets = _normalize_asset_types(gap.get("found_assets", []))
            missing_assets = _normalize_asset_types(gap.get("missing_assets", []))
            if not missing_assets and gap_required:
                missing_assets = sorted(
                    asset["type"]
                    for asset in gap_required
                    if asset["type"] not in found_assets
                )

            normalized.append(
                {
                    "question_id": question_id,
                    "branch_id": branch_id or question_context.get("branch_id", ""),
                    "question": question,
                    "intent": str(gap.get("intent") or question_context.get("intent") or "").strip(),
                    "priority": gap.get("priority", question_context.get("priority", 5)),
                    "missing_assets": missing_assets,
                    "required_assets": gap_required or context_required,
                    "expected_assets": gap_expected or context_expected,
                    "found_assets": found_assets,
                    "gap_origin": str(gap.get("gap_origin", "hunter")).strip() or "hunter",
                    "claim_id": str(gap.get("claim_id", "")).strip(),
                    "claim_ids": [
                        str(claim_id).strip()
                        for claim_id in (gap.get("claim_ids", []) or [])
                        if str(claim_id).strip()
                    ],
                    "claim_statement": str(gap.get("claim_statement", "")).strip(),
                    "support_strength": str(gap.get("support_strength", "")).strip(),
                    "search_objective": str(gap.get("search_objective", "")).strip(),
                    "existing_support_summary": str(gap.get("existing_support_summary", "")).strip(),
                    "claim_contexts": _normalize_claim_contexts(
                        gap.get("claim_contexts"),
                        fallback_claim_id=str(gap.get("claim_id", "")).strip(),
                        fallback_claim_statement=str(gap.get("claim_statement", "")).strip(),
                        fallback_support_strength=str(gap.get("support_strength", "")).strip(),
                        fallback_search_objective=str(gap.get("search_objective", "")).strip(),
                        fallback_existing_support_summary=str(gap.get("existing_support_summary", "")).strip(),
                    ),
                }
            )
        normalized.sort(key=lambda g: (g.get("priority", 5), g.get("branch_id", ""), g.get("question_id", "")))
        return normalized

    gap_branches = [b for b in branches if b.get("priority", 5) <= 2]
    if not gap_branches:
        gap_branches = list(branches)
    return [
        {
            "question_id": b.get("id", ""),
            "branch_id": b.get("id", ""),
            "question": b.get("question", ""),
            "intent": b.get("intent", ""),
            "priority": b.get("priority", 5),
            "missing_assets": [],
            "required_assets": _normalize_required_assets(b.get("required_evidence", [])),
            "expected_assets": _normalize_asset_types(b.get("expected_evidence", [])),
            "found_assets": [],
            "gap_origin": "hunter",
            "claim_id": "",
            "claim_ids": [],
            "claim_statement": "",
            "support_strength": "",
            "search_objective": "",
            "existing_support_summary": "",
            "claim_contexts": [],
        }
        for b in gap_branches
        if b.get("question")
    ]


def _normalize_asset_types(asset_types: list[str] | None) -> list[str]:
    if not asset_types:
        return []
    return [str(a).upper() for a in asset_types if isinstance(a, str) and a]


async def _build_search_queries(
    question: str,
    *,
    question_intent: str,
    missing_assets: list[str],
    required_assets: list[dict] | None = None,
    expected_assets: list[str] | None = None,
    podcast_language: str,
    mode: str = "hunter_gap_fill",
    gap_origin: str = "hunter",
    claim_statement: str = "",
    search_objective: str = "",
    existing_support_summary: str = "",
    claim_contexts: list[dict] | None = None,
) -> list[str]:
    claim_contexts = _normalize_claim_contexts(
        claim_contexts,
        fallback_claim_statement=claim_statement,
        fallback_search_objective=search_objective,
        fallback_existing_support_summary=existing_support_summary,
    )
    combined_claim_statement = " | ".join(
        context["claim_statement"] for context in claim_contexts if context.get("claim_statement")
    )
    combined_search_objective = " | ".join(
        context["search_objective"] for context in claim_contexts if context.get("search_objective")
    )
    combined_support_summary = " || ".join(
        context["existing_support_summary"] for context in claim_contexts if context.get("existing_support_summary")
    )
    try:
        synthesized_queries = await _synthesize_search_queries(
            question=question,
            question_intent=question_intent,
            missing_assets=missing_assets,
            required_assets=required_assets or [],
            expected_assets=expected_assets or [],
            podcast_language=podcast_language,
            mode=mode,
            gap_origin=gap_origin,
            claim_statement=combined_claim_statement or claim_statement,
            search_objective=combined_search_objective or search_objective,
            existing_support_summary=combined_support_summary or existing_support_summary,
            claim_contexts=claim_contexts,
        )
    except Exception:
        logger.warning("Fast web query synthesis failed for question: %s", question, exc_info=True)
        synthesized_queries = []

    if synthesized_queries:
        return synthesized_queries
    return _fallback_search_queries(
        question=question,
        mode=mode,
        claim_contexts=claim_contexts,
        claim_statement=combined_claim_statement or claim_statement,
    )


def _named_and_keyword_terms(text: str) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\w$.-]{2,}", text, flags=re.UNICODE):
        if not (any(ch.isupper() for ch in token) or any(ch.isdigit() for ch in token)):
            continue
        key = token.lower()
        if key in _FAST_WEB_STOPWORDS:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(token)
    return deduped


async def _synthesize_search_queries(
    *,
    question: str,
    question_intent: str,
    missing_assets: list[str],
    required_assets: list[dict],
    expected_assets: list[str],
    podcast_language: str,
    mode: str,
    gap_origin: str,
    claim_statement: str,
    search_objective: str,
    existing_support_summary: str,
    claim_contexts: list[dict],
) -> list[str]:
    if mode == "verifier_corroboration" or str(gap_origin).strip().lower() == "verifier":
        missing_assets = []
        required_assets = []
        expected_assets = []
    prompt = json.dumps(
        {
            "question": question,
            "question_intent": question_intent,
            "missing_assets": missing_assets,
            "required_assets": required_assets,
            "expected_assets": expected_assets,
            "podcast_language": podcast_language,
            "mode": mode,
            "gap_origin": gap_origin,
            "claim_statement": claim_statement,
            "search_objective": search_objective,
            "existing_support_summary": existing_support_summary,
            "claim_contexts": claim_contexts[:3],
            "needs_source_diversification": _needs_source_family_diversification(existing_support_summary),
        },
        ensure_ascii=False,
    )
    raw = await call_haiku_structured(
        system=FAST_WEB_QUERY_SYNTHESIS_SYSTEM,
        prompt=prompt,
        max_tokens=512,
        temperature=0.1,
    )
    parsed = parse_llm_json(raw)
    if not isinstance(parsed, dict):
        return []
    queries = parsed.get("queries", [])
    if not isinstance(queries, list):
        return []
    return _dedupe_queries([str(query).strip() for query in queries if str(query).strip()])


def _fallback_search_queries(
    *,
    question: str,
    mode: str,
    claim_contexts: list[dict],
    claim_statement: str,
) -> list[str]:
    queries: list[str] = []
    if mode == "verifier_corroboration":
        for context in claim_contexts[:2]:
            compact = _compact_search_query(str(context.get("claim_statement", "")).strip())
            if compact:
                queries.append(compact)
        if not queries and claim_statement.strip():
            queries.append(_compact_search_query(claim_statement))
    if not queries:
        queries.append(_compact_search_query(question))
    return _dedupe_queries([query for query in queries if query])


def _compact_search_query(text: str, *, max_terms: int = 10) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    for token in _named_and_keyword_terms(text):
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(token)
        if len(ordered) >= max_terms:
            return _normalize_search_query(" ".join(ordered), max_chars=_FAST_WEB_MAX_QUERY_CHARS)
    for token in re.findall(r"[\w$.-]+", text, flags=re.UNICODE):
        cleaned = str(token).strip()
        lower = cleaned.lower()
        if len(lower) < 3 or lower in _FAST_WEB_STOPWORDS or lower in seen:
            continue
        seen.add(lower)
        ordered.append(cleaned)
        if len(ordered) >= max_terms:
            break
    if len(ordered) < 2:
        return _normalize_search_query(text, max_chars=_FAST_WEB_MAX_QUERY_CHARS)
    return _normalize_search_query(" ".join(ordered), max_chars=_FAST_WEB_MAX_QUERY_CHARS)


def _dedupe_queries(queries: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        cleaned = _normalize_search_query(str(query))
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _normalize_search_query(query: str, *, max_chars: int = _FAST_WEB_MAX_QUERY_CHARS) -> str:
    cleaned = re.sub(r"\s+", " ", str(query or "")).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned

    tokens = re.findall(r"\S+", cleaned)
    kept: list[str] = []
    current_len = 0
    for token in tokens:
        normalized_token = token.strip()
        if not normalized_token:
            continue
        token_len = len(normalized_token) + (1 if kept else 0)
        if kept and current_len + token_len > max_chars:
            break
        if not kept and len(normalized_token) > max_chars:
            return normalized_token[:max_chars]
        kept.append(normalized_token)
        current_len += token_len

    normalized = " ".join(kept).strip()
    if normalized:
        return normalized
    return cleaned[:max_chars]


def _normalize_claim_contexts(
    claim_contexts: list[dict] | None,
    *,
    fallback_claim_id: str = "",
    fallback_claim_statement: str = "",
    fallback_support_strength: str = "",
    fallback_search_objective: str = "",
    fallback_existing_support_summary: str = "",
) -> list[dict]:
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for context in claim_contexts or []:
        if not isinstance(context, dict):
            continue
        claim_id = str(context.get("claim_id", "")).strip()
        claim_statement = str(context.get("claim_statement", "")).strip()
        support_strength = str(context.get("support_strength", "")).strip()
        search_objective = str(context.get("search_objective", "")).strip()
        existing_support_summary = str(context.get("existing_support_summary", "")).strip()
        if not any((claim_id, claim_statement, search_objective, existing_support_summary, support_strength)):
            continue
        dedupe_key = claim_id or claim_statement
        if dedupe_key and dedupe_key in seen_ids:
            continue
        if dedupe_key:
            seen_ids.add(dedupe_key)
        normalized.append(
            {
                "claim_id": claim_id,
                "claim_statement": claim_statement,
                "support_strength": support_strength,
                "search_objective": search_objective,
                "existing_support_summary": existing_support_summary,
            }
        )

    if normalized:
        return normalized
    if any((fallback_claim_id, fallback_claim_statement, fallback_support_strength, fallback_search_objective, fallback_existing_support_summary)):
        return [
            {
                "claim_id": fallback_claim_id,
                "claim_statement": fallback_claim_statement,
                "support_strength": fallback_support_strength,
                "search_objective": fallback_search_objective,
                "existing_support_summary": fallback_existing_support_summary,
            }
        ]
    return []


async def _run_search_queries(queries: list[str]) -> tuple[list[dict], list[dict]]:
    merged: dict[str, dict] = {}
    failures: list[dict] = []
    for query in queries:
        try:
            results = await web_search_tool(query)
        except Exception as exc:
            logger.warning("Fast web search query failed: %s", query, exc_info=True)
            failures.append(
                {
                    "query": query,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
            continue
        for result in results:
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            current = merged.get(url)
            if current is None or float(result.get("score", 0) or 0) > float(current.get("score", 0) or 0):
                merged[url] = {**result, "query_text": query}
    return list(merged.values()), failures


async def _load_known_web_source_index(db, *, project_id: str) -> dict[str, dict]:
    result = await db.execute(
        select(Source, func.count(Chunk.id))
        .outerjoin(Chunk, Chunk.source_id == Source.id)
        .where(
            Source.project_id == uuid.UUID(project_id),
            Source.source_type == SourceType.WEB,
            Source.original_url.is_not(None),
            Source.is_duplicate_of.is_(None),
        )
        .group_by(Source.id)
    )
    index: dict[str, dict] = {}
    for source, chunk_count in result.all():
        if not source.original_url:
            continue
        key = canonical_url(source.original_url)
        candidate = {
            "source_id": str(source.id),
            "url": source.original_url,
            "title": source.title or "",
            "fully_ingested": bool(source.fully_ingested),
            "chunk_count": int(chunk_count or 0),
            "has_local_content": bool(chunk_count or 0) or bool(source.text_preview),
            "text_preview": source.text_preview or "",
            "authority_type": str(source.authority_type or ""),
            "domain_reliability": str(source.domain_reliability or ""),
        }
        current = index.get(key)
        if current is None or _known_source_priority(candidate) > _known_source_priority(current):
            index[key] = candidate
    return index


def _known_source_priority(item: dict) -> tuple[int, int, int]:
    return (
        1 if item.get("fully_ingested") else 0,
        int(item.get("chunk_count", 0) or 0),
        len(str(item.get("text_preview", ""))),
    )


def _rerank_search_results(
    search_results: list[dict],
    *,
    gap: dict,
    known_web_sources: dict[str, dict],
    used_source_families: dict[str, int] | None = None,
) -> list[dict]:
    used_source_families = used_source_families or {}
    objective_terms = _gap_objective_terms(gap)

    ranked: list[dict] = []
    for result in search_results:
        url = str(result.get("url", "")).strip()
        if not url:
            continue
        title = str(result.get("title", "")).strip()
        snippet = str(result.get("snippet", "")).strip()
        canonical = canonical_url(url)
        known = known_web_sources.get(canonical)
        domain_reliability = classify_domain(url).value
        authority_type = classify_authority_heuristic(
            source_type=SourceType.WEB,
            original_url=url,
            title=title,
            text_preview=snippet,
        ).value
        source_family = _source_family(url)

        overlap_text = " ".join(part for part in (title, snippet, result.get("query_text", "")) if part)
        overlap_score = _token_overlap_score(_overlap_terms(overlap_text), objective_terms)
        rerank_score = float(result.get("score", 0) or 0)
        rerank_score += overlap_score * 0.9
        rerank_score += _source_quality_bonus(domain_reliability, authority_type)
        rerank_score += _novelty_bonus(known)
        rerank_score += _source_family_diversification_bonus(source_family, used_source_families)

        ranked.append(
            {
                **result,
                "canonical_url": canonical,
                "known_source": known,
                "domain_reliability": domain_reliability,
                "authority_type": authority_type,
                "source_family": source_family,
                "rerank_score": rerank_score,
            }
        )

    ranked.sort(key=lambda item: float(item.get("rerank_score", 0) or 0), reverse=True)
    return ranked


def _source_family(url: str) -> str:
    host = urlparse(str(url)).netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _source_family_diversification_bonus(source_family: str, used_source_families: dict[str, int]) -> float:
    if not source_family:
        return 0.0
    repeats = int(used_source_families.get(source_family, 0) or 0)
    if repeats <= 0:
        return 0.0
    return max(-0.8, -0.35 * repeats)


def _source_quality_bonus(domain_reliability: str, authority_type: str) -> float:
    reliability_bonus = {"high": 0.45, "medium": 0.15, "low": -0.1, "unknown": 0.0}.get(domain_reliability, 0.0)
    authority_bonus = {"primary": 0.5, "secondary": 0.15, "tertiary": -0.15, "unknown": 0.0}.get(authority_type, 0.0)
    return reliability_bonus + authority_bonus


def _novelty_bonus(known_source: dict | None) -> float:
    if not known_source:
        return 0.6
    if known_source.get("fully_ingested") and known_source.get("chunk_count", 0) > 0:
        return -2.5
    if known_source.get("text_preview"):
        return -0.35
    return -0.1


def _needs_source_family_diversification(existing_support_summary: str) -> bool:
    summary = existing_support_summary.lower().replace("_", "-")
    return any(
        token in summary
        for token in (
            "tertiary",
            "wikipedia",
            "secondary",
            "explainer",
            "single-source",
            "contextual-only",
            "weak-authority",
            "needs-independent-confirmation",
        )
    )


async def _prepare_gap_pages(
    *,
    gap: dict,
    ranked_results: list[dict],
    known_web_sources: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    page_limit = _FAST_WEB_VERIFIER_PAGE_LIMIT if str(gap.get("gap_origin", "")) == "verifier" else _FAST_WEB_INITIAL_PAGE_LIMIT
    page_contents: list[dict] = []
    handling: list[dict] = []
    seen_urls: set[str] = set()

    for result in ranked_results:
        if len(page_contents) >= page_limit:
            break
        url = str(result.get("url", "")).strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        known_source = result.get("known_source")
        if known_source and known_source.get("fully_ingested") and known_source.get("chunk_count", 0) > 0:
            handling.append({"url": url, "action": "skip_known_fully_ingested"})
            continue

        if known_source and not known_source.get("fully_ingested") and len(str(known_source.get("text_preview", "")).strip()) >= _FAST_WEB_MIN_PREVIEW_REUSE_CHARS:
            preview_text = str(known_source.get("text_preview", "")).strip()
            page_payload = _build_page_payload(
                url=url,
                title=str(known_source.get("title", result.get("title", ""))),
                query_text=str(result.get("query_text", "")),
                text=preview_text,
                extraction_profile="known_source_preview",
                gap=gap,
            )
            if page_payload is None:
                handling.append({"url": url, "action": "drop_known_preview_low_value"})
                continue
            page_contents.append(page_payload)
            handling.append({"url": url, "action": "reuse_known_preview"})
            continue

        if known_source and not known_source.get("fully_ingested"):
            handling.append({"url": url, "action": "skip_known_pending_no_preview"})
            continue

        page = await fetch_page_tool(url)
        if not page or page.get("error"):
            handling.append({"url": url, "action": "fetch_failed", "error": page.get("error") if isinstance(page, dict) else ""})
            continue

        page_payload = _build_page_payload(
            url=url,
            title=str(page.get("title", result.get("title", ""))),
            query_text=str(result.get("query_text", "")),
            text=str(page.get("content_markdown", "")),
            extraction_profile=str(page.get("extraction_profile", "")),
            gap=gap,
        )
        if page_payload is None:
            handling.append({"url": url, "action": "drop_low_value_page"})
            continue

        page_contents.append(page_payload)
        handling.append({"url": url, "action": "fetched", "strategy": page_payload.get("content_strategy")})

    return page_contents, handling


def _build_page_payload(
    *,
    url: str,
    title: str,
    query_text: str,
    text: str,
    extraction_profile: str,
    gap: dict,
) -> dict | None:
    cleaned = str(text or "").strip()
    if not cleaned or contains_blocking_signal(cleaned):
        return None
    if _is_low_value_page(cleaned, gap, query_text=query_text):
        return None
    excerpt_windows = _select_excerpt_windows(cleaned, gap=gap, query_text=query_text)
    if not excerpt_windows:
        return None
    return {
        "url": url,
        "title": title,
        "query_text": query_text,
        "extraction_profile": extraction_profile,
        "content_strategy": "relevance_windows",
        "excerpt_windows": excerpt_windows,
    }


def _is_low_value_page(text: str, gap: dict, *, query_text: str = "") -> bool:
    stripped = text.strip()
    if len(stripped) < _FAST_WEB_MIN_PAGE_CHARS:
        return True
    overlap = _token_overlap_score(
        _overlap_terms(text[:4000]),
        _gap_objective_terms(gap, query_text=query_text),
    )
    if overlap < _FAST_WEB_LOW_VALUE_OVERLAP_MIN and len(stripped) < 1000:
        return True
    anchor_terms = _gap_anchor_terms(gap, query_text=query_text)
    if str(gap.get("gap_origin", "")).strip().lower() == "verifier" and anchor_terms:
        anchor_hits = len(_overlap_terms(text[:5000]) & anchor_terms)
        if anchor_hits < 1:
            return True
    return False


def _select_excerpt_windows(text: str, *, gap: dict, query_text: str) -> list[str]:
    objective_terms = _gap_objective_terms(gap, query_text=query_text)
    anchor_terms = _gap_anchor_terms(gap, query_text=query_text)
    blocks = [block.strip() for block in re.split(r"\n{2,}", text) if block.strip()]
    if not blocks:
        fallback = text.strip()
        return [fallback[:_FAST_WEB_WINDOW_FALLBACK_CHARS]] if fallback else []

    windows: list[tuple[float, int, int, str]] = []
    for start_index in range(len(blocks)):
        parts: list[str] = []
        total_chars = 0
        for end_index in range(start_index, len(blocks)):
            block = blocks[end_index]
            next_chars = len(block) + (2 if parts else 0)
            if parts and total_chars + next_chars > _FAST_WEB_WINDOW_MAX_CHARS:
                break
            parts.append(block)
            total_chars += next_chars
            if total_chars < _FAST_WEB_WINDOW_MIN_CHARS and end_index < len(blocks) - 1:
                continue
            window_text = "\n\n".join(parts).strip()
            score = _score_excerpt_window(window_text, objective_terms, anchor_terms=anchor_terms)
            windows.append((score, start_index, end_index, window_text))
            break

    if not windows:
        fallback = text.strip()
        return [fallback[:_FAST_WEB_WINDOW_FALLBACK_CHARS]] if fallback else []

    selected: list[str] = []
    used_block_indexes: set[int] = set()
    for score, start_index, end_index, window_text in sorted(windows, key=lambda item: (item[0], len(item[3])), reverse=True):
        block_range = set(range(start_index, end_index + 1))
        if used_block_indexes & block_range:
            continue
        if not window_text:
            continue
        selected.append(window_text)
        used_block_indexes.update(block_range)
        if len(selected) >= _FAST_WEB_EXCERPT_MAX_WINDOWS:
            break

    if not selected:
        fallback = text.strip()
        return [fallback[:_FAST_WEB_WINDOW_FALLBACK_CHARS]] if fallback else []
    return selected


def _score_excerpt_window(text: str, objective_terms: set[str], *, anchor_terms: set[str] | None = None) -> float:
    tokens = _overlap_terms(text)
    overlap = _token_overlap_score(tokens, objective_terms)
    named_bonus = 0.1 * len(_named_and_keyword_terms(text[:1200]))
    anchor_bonus = _token_overlap_score(tokens, anchor_terms or set()) * 1.2
    return overlap + named_bonus + anchor_bonus


def _gap_objective_terms(gap: dict, *, query_text: str = "") -> set[str]:
    if str(gap.get("gap_origin", "")).strip().lower() == "verifier":
        claim_texts = [
            str(context.get("claim_statement", "")).strip()
            for context in (gap.get("claim_contexts", []) or [])
            if isinstance(context, dict)
        ]
        claim_texts = [text for text in claim_texts if text]
        terms = _overlap_terms(query_text, *claim_texts, gap.get("claim_statement", ""))
        if terms:
            return terms
    return _overlap_terms(query_text, gap.get("question", ""))


def _gap_anchor_terms(gap: dict, *, query_text: str = "") -> set[str]:
    claim_texts = [
        str(context.get("claim_statement", "")).strip()
        for context in (gap.get("claim_contexts", []) or [])
        if isinstance(context, dict)
    ]
    claim_texts = [text for text in claim_texts if text]
    if not claim_texts and gap.get("claim_statement"):
        claim_texts = [str(gap.get("claim_statement", "")).strip()]
    if not claim_texts:
        return set()

    anchors: set[str] = set()
    for text in claim_texts:
        for token in _named_and_keyword_terms(text):
            lowered = token.lower()
            if lowered in _FAST_WEB_STOPWORDS:
                continue
            anchors.add(lowered)
        for token in re.findall(r"[\w$.-]+", text, flags=re.UNICODE):
            lowered = token.lower()
            if len(lowered) < 3 or lowered in _FAST_WEB_STOPWORDS:
                continue
            if any(ch.isdigit() for ch in token):
                anchors.add(lowered)
    return anchors


def _overlap_terms(*parts: object) -> set[str]:
    tokens: set[str] = set()
    for part in parts:
        normalized = str(part or "").lower()
        for token in re.findall(r"[\w$.-]+", normalized, flags=re.UNICODE):
            if len(token) < 3:
                continue
            if token in _FAST_WEB_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def _token_overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(right), 1)


def _clip_text(value: str, limit: int = 240) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _dump_fast_web_debug(*, run_id: str, dump_type: str, payload: dict) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="fast_web",
        filename_stem=f"run-{safe_filename_token(run_id)}_{safe_filename_token(dump_type)}",
        payload=payload,
    )
