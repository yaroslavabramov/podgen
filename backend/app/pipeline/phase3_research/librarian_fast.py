"""Phase 3.2A: Librarian Fast — open-web evidence search (non-blocking)."""

import json
import logging
import uuid
import re
import unicodedata

from sqlalchemy import func, select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.models.source import Source
from app.pipeline.state import PipelineState
from app.pipeline.phase3_research.tools import web_search_tool, fetch_page_tool, create_web_evidence
from app.pipeline.phase3_research.librarian_deep import trigger_deep_ingestion
from app.services.llm import call_haiku_structured, call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.config import settings
from app.realtime.publisher import publish_event
from app.realtime.events import step_started, step_completed, evidence_found, progress

logger = logging.getLogger(__name__)

EXTRACT_WEB_EVIDENCE_SYSTEM = """You are a web evidence extractor for a podcast research pipeline.
Given web pages, a research question, question intent, and optional target_missing_assets, identify the most valuable evidence.
If target_missing_assets is non-empty, prioritize evidence that matches those asset types.

Return JSON array:
[
  {
    "url": "source URL",
    "verbatim_quote": "exact quote from the content",
    "asset_type": "STATISTIC|ANECDOTE|EXPERT_QUOTE|DEFINITION|MECHANISM|CASE|TIMELINE|CONTRAST",
    "direct_or_contextual": "direct|contextual",
    "narrative_value_score": 0.0-1.0,
    "tellability_notes": "why this evidence is valuable for a podcast"
  }
]

Use direct_or_contextual="direct" when the quote directly answers the question as asked.
Use direct_or_contextual="contextual" for background, analogy, or adjacent framing.

Max 3 evidence items per question. Only extract genuinely valuable evidence."""

FAST_WEB_QUERY_REWRITE_SYSTEM = """You generate concise English web search queries for finding strong public sources.

Return JSON:
{
  "english_query": "best general English query",
  "english_entity_query": "short high-precision English query or empty string",
  "english_official_query": "English query biased toward official / institutional / archival / legal sources or empty string"
}

Rules:
- All output queries must be in English.
- Preserve named entities, acronyms, currencies, and dates.
- Keep queries concise and search-oriented.
- Prefer wording that surfaces authoritative sources, not generic explainers.
- If a variant is not useful, return an empty string for that field.
- Return JSON only."""

_ASSET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "STATISTIC": ("statistics", "data", "figures", "official"),
    "CASE": ("case", "example", "episode", "incident"),
    "ANECDOTE": ("anecdote", "story", "example"),
    "EXPERT_QUOTE": ("expert", "analyst", "quote", "statement"),
    "DEFINITION": ("definition", "meaning", "what is"),
    "MECHANISM": ("mechanism", "how it works", "process"),
    "TIMELINE": ("timeline", "history", "chronology"),
    "CONTRAST": ("contrast", "difference", "versus", "comparison"),
}


async def research_fast_web(state: PipelineState) -> dict:
    """LangGraph node: fast web research for evidence gaps."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase3_research", "fast_web", "Searching open web for additional evidence"))

    all_evidence_ids = list(state.get("evidence_ids", []))
    iteration_counts = dict(state.get("iteration_counts", {}))
    processed_gap_count = int(
        iteration_counts.get("fast_web_gap_count", iteration_counts.get("fast_web_per_act", 0)) or 0
    )

    # Governor: max fast-web gap attempts per act/run loop.
    if processed_gap_count >= settings.max_fast_web_per_act:
        logger.info("Fast web governor hit: %d gaps processed", processed_gap_count)
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

        gap_branches = _select_gap_branches(branches, state.get("research_gaps"))
        if not gap_branches:
            logger.info("No research gaps detected for fast web step")
            return {
                "evidence_ids": all_evidence_ids,
                "iteration_counts": iteration_counts,
                "research_gaps": state.get("research_gaps", []),
                "current_phase": "phase3_research",
            }

        new_evidence_count = 0

        for gap in gap_branches:
            if processed_gap_count >= settings.max_fast_web_per_act:
                break
            processed_gap_count += 1

            question = gap["question"]
            target_missing_assets = _normalize_asset_types(gap.get("missing_assets", []))
            queries = await _build_search_queries(
                question,
                question_intent=gap.get("intent", ""),
                missing_assets=target_missing_assets,
                podcast_language=state.get("language", ""),
            )

            try:
                search_results = await _run_search_queries(queries)
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
                                "query_text": sr.get("query_text", ""),
                                "content": page.get("content_markdown", "")[:5000],
                            })
                    except Exception:
                        continue

                if not page_contents:
                    continue

                # Extract evidence with Sonnet
                prompt = json.dumps({
                    "research_question": question,
                    "question_intent": gap.get("intent"),
                    "required_evidence": gap.get("required_assets", []),
                    "expected_evidence": gap.get("expected_assets", []),
                    "target_missing_assets": target_missing_assets,
                    "web_pages": page_contents,
                }, ensure_ascii=False)

                raw = await call_sonnet_structured(
                    system=EXTRACT_WEB_EVIDENCE_SYSTEM,
                    prompt=prompt,
                    max_tokens=3072,
                    temperature=0.3,
                )
                evidence_items = parse_llm_json(raw)
                if not isinstance(evidence_items, list):
                    continue

                for item in evidence_items:
                    if not isinstance(item, dict):
                        continue
                    if not item.get("verbatim_quote") or not item.get("url"):
                        continue

                    normalized_asset_types = _normalize_asset_types([item.get("asset_type")])
                    if not normalized_asset_types:
                        logger.warning("Skipping web evidence with invalid asset_type for question: %s", question)
                        continue
                    asset_type = normalized_asset_types[0]
                    if target_missing_assets and asset_type not in target_missing_assets:
                        continue

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
                        query_text=next(
                            (page.get("query_text") for page in page_contents if page.get("url") == item["url"]),
                            question,
                        ),
                        retrieval_score=item.get("narrative_value_score"),
                        direct_or_contextual=item.get("direct_or_contextual"),
                        title=next((page.get("title") for page in page_contents if page.get("url") == item["url"]), None),
                    )
                    all_evidence_ids.append(eid)
                    new_evidence_count += 1

            except Exception:
                logger.warning("Fast web search failed for question: %s", question, exc_info=True)
                continue

        await db.commit()

    # Trigger deep ingestion for temp sources that need full indexing.
    #
    # Concurrency contract (single source of truth = DB status):
    # - Worker sets PROCESSING atomically (SELECT FOR UPDATE SKIP LOCKED)
    # - We count PROCESSING sources in DB to know real concurrency
    # - Duplicate Celery tasks are harmless: worker skips if already PROCESSING/COMPLETED
    # - No Celery task ID tracking needed in state
    from app.models.source import IngestionStatus
    async with async_session_factory() as db2:
        # Count currently active deep ingestions from DB (ENQUEUED + PROCESSING)
        active_result = await db2.execute(
            select(func.count(Source.id)).where(
                Source.generation_run_id == uuid.UUID(state["generation_run_id"]),
                Source.is_temporary.is_(True),
                Source.ingestion_status.in_([IngestionStatus.ENQUEUED, IngestionStatus.PROCESSING]),
            )
        )
        deep_active = active_result.scalar() or 0

        if deep_active < settings.max_deep_ingestion_concurrent:
            # Find sources not yet picked up by any worker
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

    iteration_counts["deep_ingestion_active"] = deep_active
    iteration_counts["fast_web_gap_count"] = processed_gap_count
    iteration_counts["fast_web_per_act"] = processed_gap_count
    publish_event(run_id, step_completed("phase3_research", "fast_web"))
    if new_evidence_count > 0:
        publish_event(run_id, evidence_found(new_evidence_count, ["web"]))
        publish_event(run_id, progress(33.0, f"Found {new_evidence_count} additional web evidence"))

    return {
        "evidence_ids": all_evidence_ids,
        "iteration_counts": iteration_counts,
        "research_gaps": state.get("research_gaps", []),
        "current_phase": "phase3_research",
    }


def _select_gap_branches(branches: list[dict], research_gaps: list[dict] | None) -> list[dict]:
    """Prefer explicit Hunter gaps; fallback to priority heuristic for compatibility."""
    if isinstance(research_gaps, list):
        if not research_gaps:
            return []

        normalized = []
        for gap in research_gaps:
            if not isinstance(gap, dict):
                continue
            question = gap.get("question")
            if not question:
                continue
            normalized.append({
                "question_id": gap.get("question_id", ""),
                "branch_id": gap.get("branch_id", ""),
                "question": question,
                "intent": gap.get("intent", ""),
                "priority": gap.get("priority", 5),
                "missing_assets": _normalize_asset_types(gap.get("missing_assets", [])),
                "required_assets": gap.get("required_assets", []),
                "expected_assets": _normalize_asset_types(gap.get("expected_assets", [])),
            })
        normalized.sort(key=lambda g: (g.get("priority", 5), g.get("branch_id", "")))
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
            "required_assets": b.get("required_evidence", []),
            "expected_assets": _normalize_asset_types(b.get("expected_evidence", [])),
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
    podcast_language: str,
) -> list[str]:
    native_queries = _build_native_search_queries(question, missing_assets)
    if not _needs_english_web_rewrite(question, podcast_language):
        return native_queries

    try:
        english_queries = await _rewrite_queries_to_english(
            question=question,
            question_intent=question_intent,
            missing_assets=missing_assets,
        )
    except Exception:
        logger.warning("Fast web English query rewrite failed for question: %s", question, exc_info=True)
        english_queries = []

    return _dedupe_queries([*english_queries, *native_queries])


def _build_native_search_queries(question: str, missing_assets: list[str]) -> list[str]:
    queries = [question]
    targeted = _build_targeted_query(question, missing_assets)
    if targeted and targeted != question:
        queries.append(targeted)
    return _dedupe_queries(queries)


def _build_targeted_query(question: str, missing_assets: list[str]) -> str:
    keywords: list[str] = []
    for asset_type in missing_assets:
        keywords.extend(_ASSET_KEYWORDS.get(asset_type, (asset_type.lower(),)))

    named_tokens = [
        token
        for token in re.findall(r"[\w$.-]{2,}", question, flags=re.UNICODE)
        if any(ch.isupper() for ch in token) or any(ch.isdigit() for ch in token)
    ]
    parts = [question, *named_tokens, *keywords]
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = str(part).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return " ".join(deduped).strip()


def _needs_english_web_rewrite(question: str, podcast_language: str) -> bool:
    if str(podcast_language or "").strip().lower() not in {"", "en"}:
        return True
    for char in question:
        if not char.isalpha():
            continue
        try:
            if "LATIN" not in unicodedata.name(char):
                return True
        except ValueError:
            return True
    return False


async def _rewrite_queries_to_english(
    *,
    question: str,
    question_intent: str,
    missing_assets: list[str],
) -> list[str]:
    prompt = json.dumps(
        {
            "question": question,
            "question_intent": question_intent,
            "missing_assets": missing_assets,
        },
        ensure_ascii=False,
    )
    raw = await call_haiku_structured(
        system=FAST_WEB_QUERY_REWRITE_SYSTEM,
        prompt=prompt,
        max_tokens=512,
        temperature=0.2,
    )
    parsed = parse_llm_json(raw)
    if not isinstance(parsed, dict):
        return []
    return _dedupe_queries(
        [
            str(parsed.get("english_query", "")).strip(),
            str(parsed.get("english_entity_query", "")).strip(),
            str(parsed.get("english_official_query", "")).strip(),
        ]
    )


def _dedupe_queries(queries: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        cleaned = str(query).strip()
        if not cleaned:
            continue
        key = re.sub(r"\s+", " ", cleaned).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


async def _run_search_queries(queries: list[str]) -> list[dict]:
    merged: dict[str, dict] = {}
    for query in queries:
        for result in await web_search_tool(query):
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            current = merged.get(url)
            if current is None or float(result.get("score", 0) or 0) > float(current.get("score", 0) or 0):
                merged[url] = {
                    **result,
                    "query_text": query,
                }
    return sorted(merged.values(), key=lambda item: float(item.get("score", 0) or 0), reverse=True)
