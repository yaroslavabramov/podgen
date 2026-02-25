"""ContentMap builder: aggregates topics, entities, and key terms from project chunks."""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

import tiktoken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.chunker import OVERLAP_TOKENS
from app.models.chunk import Chunk
from app.models.content_map import ContentMap
from app.models.source import Source
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope
from app.pipeline.llm_json import parse_llm_json
from app.services.llm import call_haiku_structured

logger = logging.getLogger(__name__)

# Maximum number of source summaries to include in a single LLM call.
_MAX_SOURCE_SUMMARIES = 80
# Adaptive section limits per source summary.
_MAX_SECTIONS_PER_SOURCE_BASE = 12
_MAX_SECTIONS_PER_SOURCE_MEDIUM = 18
_MAX_SECTIONS_PER_SOURCE_FEW = 24
# Max chars for section lead.
_MAX_LEAD_CHARS = 260
_MAX_HEADING_CLUSTERS = 40
_LEAD_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s*")
_LEAD_BAD_PREFIX_RE = re.compile(r"^[,;:)\]\-]")
_LEAD_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_URL_MARKER_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]{1,120}\]\([^)]+\)")
_SENTENCE_END_RE = re.compile(r"[!?。！？]|(?<!\d)\.(?=\s|$)")
_LIST_SEPARATOR_RE = re.compile(r"[,;|•●·，；、｜]")
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-я0-9]+|[\u3400-\u9fff]+|[\u3040-\u30ff]+|[\uac00-\ud7af]+")
_CJK_TEXT_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_BULLET_MARKER_RE = re.compile(r"(?:^|\s)[*•●]\s+")
_lead_encoder: tiktoken.Encoding | None = None


async def build_content_map(project_id: str, db: AsyncSession) -> str:
    """Build a ContentMap from all project chunks.

    Aggregates source-level structure (sections + leads), coverage signals, and
    heading clusters from chunk data. Uses Haiku structured output to produce the map.

    Returns:
        The content_map_id (UUID string) of the newly created ContentMap.
    """
    trace_ctx = {
        "component": "ingestion.scout",
        "project_id": project_id,
    }
    with push_trace_context(trace_ctx):
        with tracing_scope(metadata=trace_ctx, tags=["ingestion", "scout"]):
            with trace_span(
                "ingestion.scout.build_content_map",
                run_type="chain",
                inputs={"project_id": project_id},
            ) as scout_run:
                # --- 1. Gather source+chunk rows -----------------------------------
                with trace_span(
                    "ingestion.scout.collect_chunks",
                    run_type="retriever",
                    inputs={"project_id": project_id},
                ) as collect_run:
                    result = await db.execute(
                        select(
                            Chunk,
                            Source.id,
                            Source.title,
                            Source.source_type,
                            Source.original_url,
                            Source.published_at,
                        )
                        .join(Source, Chunk.source_id == Source.id)
                        .where(Source.project_id == project_id)
                        .order_by(Source.id, Chunk.chunk_index)
                    )
                    rows = result.all()
                    safe_set_run(
                        collect_run,
                        outputs={
                            "rows_count": len(rows),
                            "source_count": len({str(row[1]) for row in rows}),
                        },
                    )

                if not rows:
                    safe_set_run(scout_run, outputs={"status": "no_chunks"})
                    raise ValueError(f"No chunks found for project {project_id}")

                with trace_span(
                    "ingestion.scout.build_source_summaries",
                    run_type="chain",
                    inputs={"rows_count": len(rows), "max_sources": _MAX_SOURCE_SUMMARIES},
                ) as summaries_run:
                    source_summaries = _build_source_summaries(rows)
                    heading_clusters = _build_heading_clusters(source_summaries, top_n=_MAX_HEADING_CLUSTERS)
                    safe_set_run(
                        summaries_run,
                        outputs={
                            "source_summary_count": len(source_summaries),
                            "heading_cluster_count": len(heading_clusters),
                            "truncated_sources": len({str(row[1]) for row in rows}) > _MAX_SOURCE_SUMMARIES,
                        },
                    )

                logger.info(
                    "Building content map for project %s with %d source summaries",
                    project_id,
                    len(source_summaries),
                )

                # --- 2. Call Haiku for structured extraction ------------------------
                with trace_span(
                    "ingestion.scout.extract_map",
                    run_type="chain",
                    inputs={
                        "source_summary_count": len(source_summaries),
                        "heading_cluster_count": len(heading_clusters),
                    },
                ) as extract_run:
                    map_json = await _extract_content_map(source_summaries, heading_clusters)
                    safe_set_run(
                        extract_run,
                        outputs={
                            "topic_count": _len_if_list(map_json.get("topics")),
                            "entity_count": _len_if_list(map_json.get("entities")),
                            "key_term_count": _len_if_list(map_json.get("key_terms")),
                            "gap_count": _len_if_list(map_json.get("gaps")),
                            "parse_warning": bool(map_json.get("_raw_response")),
                        },
                    )

                # --- 3. Persist ContentMap -----------------------------------------
                with trace_span(
                    "ingestion.scout.persist_map",
                    run_type="chain",
                    inputs={"project_id": project_id},
                ) as persist_run:
                    content_map = ContentMap(
                        id=uuid.uuid4(),
                        project_id=uuid.UUID(project_id),
                        map_json=map_json,
                    )
                    db.add(content_map)
                    await db.flush()
                    content_map_id = str(content_map.id)
                    safe_set_run(persist_run, outputs={"content_map_id": content_map_id})

                safe_set_run(
                    scout_run,
                    outputs={
                        "status": "ok",
                        "content_map_id": content_map_id,
                        "source_summary_count": len(source_summaries),
                        "heading_cluster_count": len(heading_clusters),
                    },
                )
                logger.info("Created ContentMap %s for project %s", content_map_id, project_id)
                return content_map_id


def _build_source_summaries(rows) -> list[dict]:
    """Build source-level structured summaries for the LLM prompt."""
    by_source: dict[str, dict[str, Any]] = {}

    for row in rows:
        chunk: Chunk = row[0]
        source_id = str(row[1])
        source_title: str | None = row[2]
        source_type: str = row[3]
        source_url: str | None = row[4]
        published_at: datetime | None = row[5]

        source = by_source.get(source_id)
        if source is None:
            source = {
                "source_id": source_id,
                "source": source_title or "Untitled",
                "type": source_type,
                "url": source_url,
                "published_at": published_at.date().isoformat() if isinstance(published_at, datetime) else None,
                "total_tokens": 0,
                "chunk_count": 0,
                "_structure_depth": 0,
                "_sections": {},
                "_root_sections": [],
                "_prev_base_chunk_content": None,
            }
            by_source[source_id] = source

        base_chunk_content = _strip_overlap_prefix_for_lead(
            chunk.content,
            source.get("_prev_base_chunk_content"),
        )
        source["_prev_base_chunk_content"] = base_chunk_content
        candidate_lead = _extract_lead(base_chunk_content)

        token_count = int(chunk.token_count or 0)
        source["total_tokens"] += token_count
        source["chunk_count"] += 1

        headings = [h.strip() for h in (chunk.heading_path or []) if isinstance(h, str) and h.strip()]
        source["_structure_depth"] = max(source["_structure_depth"], len(headings))
        if headings:
            section_key = " > ".join(headings)
        else:
            # Keep root-only chunks separate so unstructured long sources still expose
            # meaningful coverage in scout summaries.
            root_key = f"(root:{int(chunk.chunk_index)})"
            source["_root_sections"].append(
                {
                    "key": root_key,
                    "headings": [],
                    "lead": candidate_lead,
                    "_lead_score": _lead_quality_score(candidate_lead, []),
                    "token_count": token_count,
                    "chunk_count": 1,
                    "chunk_index": int(chunk.chunk_index or 0),
                }
            )
            section_key = root_key
        section = source["_sections"].get(section_key)
        if section is None:
            section = {
                "headings": headings,
                "lead": candidate_lead,
                "_lead_score": _lead_quality_score(candidate_lead, headings),
                "token_count": 0,
                "chunk_count": 0,
                "chunk_index": int(chunk.chunk_index or 0),
            }
            source["_sections"][section_key] = section
        else:
            best_score = section.get("_lead_score")
            candidate_score = _lead_quality_score(candidate_lead, headings)
            if best_score is None or candidate_score > best_score:
                section["lead"] = candidate_lead
                section["_lead_score"] = candidate_score

        section["token_count"] += token_count
        section["chunk_count"] += 1

    source_candidates: list[dict[str, Any]] = []
    for source in by_source.values():
        if source["_structure_depth"] == 0 and source["_root_sections"]:
            section_pool = sorted(source["_root_sections"], key=lambda item: int(item["chunk_index"]))
        else:
            section_pool = list(source["_sections"].values())
            section_pool.sort(key=lambda item: (int(item["token_count"]), len(item["headings"])), reverse=True)

        kept_sections, removed_sections, removed_tokens, noise_signal_ratio, prose_section_ratio = _filter_sections_for_source(
            source_type=str(source["type"]),
            sections=section_pool,
        )
        kept_tokens = max(0, int(source["total_tokens"]) - int(removed_tokens))

        quality_weight = _source_quality_weight(
            source_type=str(source["type"]),
            total_tokens=int(source["total_tokens"]),
            kept_tokens=kept_tokens,
            kept_sections=len(kept_sections),
            removed_sections=removed_sections,
            noise_signal_ratio=noise_signal_ratio,
            prose_section_ratio=prose_section_ratio,
        )
        if quality_weight <= 0.0:
            continue

        source_candidates.append(
            {
                "source": source,
                "sections": kept_sections,
                "kept_tokens": kept_tokens,
                "quality_weight": quality_weight,
                "effective_tokens": int(round(kept_tokens * quality_weight)),
                "structure_depth": max((len(section.get("headings", [])) for section in kept_sections), default=0),
            }
        )

    source_candidates.sort(
        key=lambda item: (
            int(item["effective_tokens"]),
            int(item["source"]["chunk_count"]),
            int(item["kept_tokens"]),
        ),
        reverse=True,
    )
    source_candidates = source_candidates[:_MAX_SOURCE_SUMMARIES]

    summaries: list[dict] = []
    total_sources = len(source_candidates)
    for candidate in source_candidates:
        source = candidate["source"]
        section_limit = _section_limit_for_source(
            chunk_count=int(source["chunk_count"]),
            total_sources=total_sources,
        )
        section_values = candidate["sections"][:section_limit]
        if not section_values:
            continue

        summaries.append(
            {
                "source_id": source["source_id"],
                "source": source["source"],
                "type": source["type"],
                "url": source["url"],
                "published_at": source["published_at"],
                "total_tokens": candidate["kept_tokens"],
                "chunk_count": source["chunk_count"],
                "section_count": len(candidate["sections"]),
                "structure_depth": candidate["structure_depth"],
                "sections": [
                    {
                        "headings": section["headings"],
                        "lead": section["lead"],
                        "token_count": section["token_count"],
                    }
                    for section in section_values
                ],
            }
        )

    return summaries


def _get_lead_encoder() -> tiktoken.Encoding:
    global _lead_encoder
    if _lead_encoder is None:
        _lead_encoder = tiktoken.get_encoding("cl100k_base")
    return _lead_encoder


def _strip_overlap_prefix_for_lead(text: str, prev_base_chunk_content: str | None) -> str:
    """Remove chunk overlap prefix so scout lead starts from chunk's own body."""
    if not text or not prev_base_chunk_content:
        return text

    encoder = _get_lead_encoder()
    prev_tokens = encoder.encode(prev_base_chunk_content)
    if not prev_tokens:
        return text

    overlap_tokens = prev_tokens[-OVERLAP_TOKENS:] if len(prev_tokens) > OVERLAP_TOKENS else prev_tokens
    overlap_text = encoder.decode(overlap_tokens).strip()
    if not overlap_text:
        return text

    prefix = f"{overlap_text}\n\n"
    if not text.startswith(prefix):
        return text

    trimmed = text[len(prefix) :].lstrip()
    return trimmed or text


def _extract_lead(text: str) -> str:
    """Extract a compact lead sentence from a chunk."""
    if not text:
        return ""

    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""

    parts = [part.strip() for part in _LEAD_SENTENCE_SPLIT_RE.split(normalized) if part.strip()]
    if not parts:
        return normalized[:_MAX_LEAD_CHARS]

    preferred_candidates: list[tuple[int, str]] = []
    fallback_candidates: list[tuple[int, str]] = []
    for idx, candidate in enumerate(parts[:12]):
        if _is_preferred_lead_candidate(candidate):
            preferred_candidates.append((idx, candidate))
            continue
        if _is_fallback_lead_candidate(candidate):
            fallback_candidates.append((idx, candidate))

    if preferred_candidates:
        best = max(preferred_candidates, key=lambda item: _lead_candidate_score(item[1], item[0]))[1]
        return best[:_MAX_LEAD_CHARS]

    if fallback_candidates:
        best = max(fallback_candidates, key=lambda item: _lead_candidate_score(item[1], item[0]))[1]
        return best[:_MAX_LEAD_CHARS]

    # Last resort: first non-empty fragment.
    return parts[0][:_MAX_LEAD_CHARS]


def _lead_quality_score(lead: str, headings: list[str]) -> tuple[int, int, int, int, int, int, int]:
    if not lead:
        return (0, 0, 0, 0, -999, 0, 0)

    section = {"headings": headings, "lead": lead, "token_count": 0}
    noise_score = _navigation_noise_score(
        section,
        context={"total_sections": 1, "prefix_counts": {}},
    )
    preferred = 1 if _is_preferred_lead_candidate(lead) else 0
    fallback = 1 if preferred or _is_fallback_lead_candidate(lead) else 0
    prose = 1 if _is_prose_like_section(section) else 0
    sentence_ends = _count_sentence_end_punct(lead)
    units = _lead_content_units(lead)
    return (
        preferred,
        fallback,
        prose,
        sentence_ends,
        -noise_score,
        units,
        len(lead),
    )


def _is_preferred_lead_candidate(candidate: str) -> bool:
    if len(candidate) < _min_lead_chars(candidate, preferred=True):
        return False
    if _LEAD_BAD_PREFIX_RE.match(candidate):
        return False
    if not _has_preferred_start(candidate):
        return False
    units = _lead_content_units(candidate)
    return units >= 6


def _is_fallback_lead_candidate(candidate: str) -> bool:
    if len(candidate) < _min_lead_chars(candidate, preferred=False):
        return False
    if _LEAD_BAD_PREFIX_RE.match(candidate):
        return False
    if _starts_with_caseful_lower_letter(candidate):
        return False
    units = _lead_content_units(candidate)
    return units >= 5


def _has_preferred_start(candidate: str) -> bool:
    first = _first_significant_char(candidate)
    if first is None:
        return False
    if first.isdigit():
        return True
    if _is_caseful_letter(first):
        return first.isupper()
    return first.isalpha() or bool(_CJK_TEXT_RE.match(first))


def _first_significant_char(candidate: str) -> str | None:
    stripped = candidate.lstrip("\"“”'‘’([")
    if not stripped:
        return None
    return stripped[0]


def _is_caseful_letter(char: str) -> bool:
    return char.isalpha() and char.lower() != char.upper()


def _starts_with_caseful_lower_letter(candidate: str) -> bool:
    first = _first_significant_char(candidate)
    if first is None or not _is_caseful_letter(first):
        return False
    return first.islower()


def _lead_content_units(candidate: str) -> int:
    word_units = len(_LEAD_WORD_RE.findall(candidate))
    cjk_chars = len(_CJK_TEXT_RE.findall(candidate))
    cjk_units = cjk_chars // 2
    # For scripts without case and/or whitespace token boundaries (for example Thai),
    # fall back to alphabetic char density so informative sentences aren't penalized.
    alpha_chars = sum(1 for char in candidate if char.isalpha())
    script_units = alpha_chars // 6
    return max(word_units, cjk_units, script_units)


def _lead_candidate_score(candidate: str, position: int) -> tuple[int, int, int]:
    position_bonus = 1 if position > 0 else 0
    return (_lead_content_units(candidate) + position_bonus, len(candidate), position)


def _min_lead_chars(candidate: str, *, preferred: bool) -> int:
    cjk_chars = len(_CJK_TEXT_RE.findall(candidate))
    if cjk_chars >= 8:
        return 12 if preferred else 10
    return 35 if preferred else 30


def _section_limit_for_source(*, chunk_count: int, total_sources: int) -> int:
    """Adaptive section cap: higher for few-source projects, tighter for many-source projects."""
    if chunk_count <= _MAX_SECTIONS_PER_SOURCE_BASE:
        return max(1, chunk_count)

    if total_sources <= 5:
        hard_cap = _MAX_SECTIONS_PER_SOURCE_FEW
    elif total_sources <= 15:
        hard_cap = _MAX_SECTIONS_PER_SOURCE_MEDIUM
    else:
        hard_cap = _MAX_SECTIONS_PER_SOURCE_BASE

    if hard_cap <= _MAX_SECTIONS_PER_SOURCE_BASE:
        return _MAX_SECTIONS_PER_SOURCE_BASE

    ratio = min(1.0, chunk_count / 24.0)
    scaled = _MAX_SECTIONS_PER_SOURCE_BASE + int(
        round((hard_cap - _MAX_SECTIONS_PER_SOURCE_BASE) * ratio)
    )
    return max(_MAX_SECTIONS_PER_SOURCE_BASE, min(hard_cap, scaled))


def _filter_sections_for_source(
    *,
    source_type: str,
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int, float, float]:
    if source_type.lower() != "web":
        return sections, 0, 0, 0.0, 1.0

    context = _build_section_noise_context(sections)
    kept: list[dict[str, Any]] = []
    removed_count = 0
    removed_tokens = 0
    noisy_hint_sections = 0
    prose_like_sections = 0
    for section in sections:
        if _is_prose_like_section(section):
            prose_like_sections += 1
        noise_score = _navigation_noise_score(section, context=context)
        if noise_score >= 2:
            noisy_hint_sections += 1
        if noise_score >= 4:
            removed_count += 1
            removed_tokens += int(section.get("token_count") or 0)
            continue
        kept.append(section)
    hint_ratio = noisy_hint_sections / max(1, len(sections))
    prose_ratio = prose_like_sections / max(1, len(sections))
    return kept, removed_count, removed_tokens, hint_ratio, prose_ratio


def _build_section_noise_context(sections: list[dict[str, Any]]) -> dict[str, Any]:
    prefix_counts: dict[str, int] = {}
    for section in sections:
        headings = _normalized_headings(section.get("headings", []))
        for depth in range(1, len(headings) + 1):
            key = " > ".join(headings[:depth])
            prefix_counts[key] = prefix_counts.get(key, 0) + 1

    return {
        "total_sections": len(sections),
        "prefix_counts": prefix_counts,
    }


def _normalized_headings(raw_headings: Any) -> list[str]:
    headings: list[str] = []
    if not isinstance(raw_headings, list):
        return headings
    for heading in raw_headings:
        normalized = re.sub(r"\s+", " ", str(heading or "")).strip().lower()
        if normalized:
            headings.append(normalized)
    return headings


def _tokenize_for_noise(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _heading_word_count(heading: str) -> int:
    return len([part for part in re.split(r"\s+", heading.strip()) if part])


def _is_prose_like_section(section: dict[str, Any]) -> bool:
    lead = str(section.get("lead") or "")
    tokens = _tokenize_for_noise(lead)
    token_count = len(tokens)
    if token_count < 8:
        return False
    sentence_punct_count = _count_sentence_end_punct(lead)
    separator_count = len(_LIST_SEPARATOR_RE.findall(lead))
    titlecase_ratio = _titlecase_ratio(tokens)
    if sentence_punct_count >= 1:
        return True
    # Longer lines can still be prose when sentence punctuation is missing due truncation.
    if token_count >= 24 and separator_count <= 1 and titlecase_ratio <= 0.65:
        return True
    if token_count >= 14 and _CJK_TEXT_RE.search(lead) and separator_count <= 1 and sentence_punct_count >= 1:
        return True
    return False


def _is_navigation_noise_section(section: dict[str, Any], *, context: dict[str, Any] | None = None) -> bool:
    return _navigation_noise_score(section, context=context) >= 4


def _navigation_noise_score(section: dict[str, Any], *, context: dict[str, Any] | None = None) -> int:
    score = 0

    if context is None:
        context = {"total_sections": 1, "prefix_counts": {}}

    headings = _normalized_headings(section.get("headings", []))
    depth = len(headings)
    if depth >= 5:
        score += 3
    elif depth == 4:
        score += 2
    elif depth == 3:
        score += 1

    if depth >= 3:
        short_heading_count = sum(1 for heading in headings if _heading_word_count(heading) <= 2)
        if short_heading_count == depth:
            score += 1
        elif depth >= 4 and short_heading_count >= depth - 1:
            score += 1

    total_sections = int(context.get("total_sections") or 0)
    prefix_counts = context.get("prefix_counts", {})
    if total_sections >= 4 and depth >= 2 and isinstance(prefix_counts, dict):
        for prefix_depth in range(2, min(depth, 3) + 1):
            key = " > ".join(headings[:prefix_depth])
            freq = int(prefix_counts.get(key) or 0) / max(1, total_sections)
            if freq >= 0.6:
                score += 1
                break

    lead = str(section.get("lead") or "")
    tokens = _tokenize_for_noise(lead)
    token_count = len(tokens)
    sentence_punct_count = _count_sentence_end_punct(lead)
    separator_count = len(_LIST_SEPARATOR_RE.findall(lead))
    url_marker_count = len(_URL_MARKER_RE.findall(lead)) + len(_MARKDOWN_LINK_RE.findall(lead))
    bullet_marker_count = len(_BULLET_MARKER_RE.findall(lead))
    if url_marker_count > 0:
        score += 2
    if bullet_marker_count >= 2 and sentence_punct_count == 0:
        score += 2

    # List/taxonomy-like signal: many tokens, little prose punctuation.
    if token_count >= 16 and sentence_punct_count == 0 and separator_count >= 2:
        score += 2
    if token_count >= 24 and sentence_punct_count <= 1 and separator_count >= 4:
        score += 1
    titlecase_ratio = _titlecase_ratio(tokens)
    if token_count >= 20 and sentence_punct_count == 0 and separator_count <= 1 and titlecase_ratio >= 0.72:
        score += 2
    if _camelcase_noise_tokens(tokens) >= 2 and sentence_punct_count <= 1:
        score += 1

    cjk_char_count = len(_CJK_TEXT_RE.findall(lead))
    cjk_token_count = sum(1 for token in tokens if _CJK_TEXT_RE.search(token))
    if cjk_char_count >= 10 and sentence_punct_count == 0 and separator_count >= 2 and cjk_token_count >= 4:
        score += 2
    if cjk_char_count >= 16 and sentence_punct_count <= 1 and separator_count >= 4 and cjk_token_count >= 6:
        score += 2
    if cjk_char_count >= 16 and sentence_punct_count == 0 and separator_count == 0 and cjk_token_count >= 6:
        score += 1

    alpha_tokens = 0
    titlecase_like = 0
    for token in tokens:
        first_alpha = next((ch for ch in token if ch.isalpha()), "")
        if not first_alpha:
            continue
        alpha_tokens += 1
        if first_alpha.isupper():
            titlecase_like += 1

    # Name/index lists (countries/topics) often have no sentence punctuation and mostly title-cased tokens.
    if alpha_tokens >= 8 and sentence_punct_count == 0:
        titlecase_ratio = titlecase_like / max(1, alpha_tokens)
        if titlecase_ratio >= 0.8 and separator_count == 0:
            score += 2
        elif titlecase_ratio >= 0.7:
            score += 1

    # Prose signal reduces false positives (e.g. legitimate "Metrics"/"Keywords" sections).
    if sentence_punct_count >= 1 and token_count >= 8:
        score -= 1
    if sentence_punct_count >= 2:
        score -= 1

    return score


def _source_quality_weight(
    *,
    source_type: str,
    total_tokens: int,
    kept_tokens: int,
    kept_sections: int,
    removed_sections: int,
    noise_signal_ratio: float,
    prose_section_ratio: float,
) -> float:
    if source_type.lower() != "web":
        return 1.0
    if kept_sections <= 0 or kept_tokens <= 0:
        return 0.0

    if noise_signal_ratio >= 0.9 and kept_tokens < 1200 and prose_section_ratio < 0.5:
        return 0.0
    if noise_signal_ratio >= 0.75 and kept_tokens < 1200:
        return 0.0
    if prose_section_ratio < 0.2 and noise_signal_ratio >= 0.66 and kept_sections <= 3:
        return 0.0
    if prose_section_ratio == 0.0 and noise_signal_ratio >= 0.5 and kept_sections <= 2:
        return 0.0

    noise_ratio = 1.0 - (kept_tokens / max(1, total_tokens))
    if noise_ratio >= 0.9 and kept_tokens < 900 and prose_section_ratio < 0.5:
        return 0.0
    if kept_sections == 1 and removed_sections >= 4 and kept_tokens < 900:
        return 0.0

    weight = 1.0
    if noise_ratio >= 0.75:
        weight = min(weight, 0.3)
    elif noise_ratio >= 0.5:
        weight = min(weight, 0.6)
    elif noise_ratio >= 0.3:
        weight = min(weight, 0.8)

    if noise_signal_ratio >= 0.75:
        weight = min(weight, 0.3)
    elif noise_signal_ratio >= 0.6:
        weight = min(weight, 0.6)
    elif noise_signal_ratio >= 0.4:
        weight = min(weight, 0.8)
    return weight


def _count_sentence_end_punct(text: str) -> int:
    return len(_SENTENCE_END_RE.findall(text))


def _titlecase_ratio(tokens: list[str]) -> float:
    alpha_tokens = 0
    titlecase_like = 0
    for token in tokens:
        first_alpha = next((ch for ch in token if ch.isalpha()), "")
        if not first_alpha:
            continue
        alpha_tokens += 1
        if first_alpha.isupper():
            titlecase_like += 1
    return titlecase_like / max(1, alpha_tokens)


def _camelcase_noise_tokens(tokens: list[str]) -> int:
    count = 0
    for token in tokens:
        if len(token) < 14:
            continue
        if not any(ch.islower() for ch in token) or not any(ch.isupper() for ch in token):
            continue
        inner_upper = sum(1 for ch in token[1:] if ch.isupper())
        if inner_upper >= 2:
            count += 1
    return count


def _build_heading_clusters(source_summaries: list[dict], top_n: int = _MAX_HEADING_CLUSTERS) -> list[dict]:
    """Build lightweight cross-source heading clusters to reduce duplicate themes."""
    cluster_map: dict[str, dict[str, Any]] = {}

    for source in source_summaries:
        source_id = str(source.get("source_id") or "")
        for section in source.get("sections", []):
            headings = section.get("headings", [])
            if not isinstance(headings, list) or not headings:
                continue

            raw_heading = str(headings[-1]).strip()
            if not raw_heading:
                continue

            normalized = re.sub(r"\s+", " ", raw_heading).strip().lower()
            if not normalized:
                continue

            cluster = cluster_map.get(normalized)
            if cluster is None:
                cluster = {
                    "heading": raw_heading,
                    "mentions": 0,
                    "source_ids": set(),
                }
                cluster_map[normalized] = cluster

            cluster["mentions"] += 1
            if source_id:
                cluster["source_ids"].add(source_id)

    clusters = [
        {
            "heading": cluster["heading"],
            "mentions": cluster["mentions"],
            "source_count": len(cluster["source_ids"]),
        }
        for cluster in cluster_map.values()
        if len(cluster["source_ids"]) > 1
    ]
    clusters.sort(key=lambda item: (int(item["source_count"]), int(item["mentions"])), reverse=True)
    return clusters[:top_n]


async def _extract_content_map(
    source_summaries: list[dict],
    heading_clusters: list[dict],
) -> dict:
    """Use Haiku to extract a structured content map from source-level summaries."""
    with trace_span(
        "ingestion.scout.extract_content_map",
        run_type="chain",
        inputs={
            "source_summary_count": len(source_summaries),
            "heading_cluster_count": len(heading_clusters),
        },
    ) as extract_run:
        payload = {
            "source_summaries": source_summaries,
            "heading_clusters": heading_clusters,
        }
        payload_json = json.dumps(payload, ensure_ascii=False)
        trimmed = False

        # If payload is too large, progressively trim source_summaries.
        if len(payload_json) > 80_000:
            ratio = 80_000 / len(payload_json)
            trimmed_count = max(5, int(len(source_summaries) * ratio))
            source_summaries = source_summaries[:trimmed_count]
            payload = {
                "source_summaries": source_summaries,
                "heading_clusters": heading_clusters,
            }
            payload_json = json.dumps(payload, ensure_ascii=False)
            trimmed = True

        system_prompt = (
            "You are a content analyst. Given source-level summaries from a research project, "
            "produce a structured content map that identifies the key themes and information "
            "available across all sources.\n\n"
            "Return a JSON object with this schema:\n"
            "{\n"
            '  "topics": [{"name": str, "description": str, "chunk_count": int}],\n'
            '  "entities": [{"name": str, "type": str, "mentions": int}],\n'
            '  "key_terms": [{"term": str, "context": str}],\n'
            '  "source_coverage": [{"source": str, "topics_covered": [str]}],\n'
            '  "gaps": [str],\n'
            '  "summary": str\n'
            "}\n\n"
            "- Input is source-level: each source contains sections (heading path + lead) and coverage signals.\n"
            "- topics: Major themes with approximate chunk counts.\n"
            "- entities: People, organizations, technologies, etc.\n"
            "- key_terms: Domain-specific terms with brief context.\n"
            "- source_coverage: Which sources cover which topics.\n"
            "- gaps: Potential information gaps that might need more sources.\n"
            "- summary: A 2-3 sentence overview of the project's content landscape.\n"
            "- Use heading_clusters to merge duplicate section names across sources."
        )

        user_prompt = (
            f"Analyze these {len(source_summaries)} source summaries and produce the content map:\n\n"
            f"{payload_json}"
        )

        raw = await call_haiku_structured(
            system=system_prompt,
            prompt=user_prompt,
            max_tokens=4096,
            temperature=0.2,
        )

        parse_ok = False
        try:
            parsed = parse_llm_json(raw)
            if isinstance(parsed, dict):
                result = parsed
                parse_ok = True
            else:
                raise ValueError("Content map response must be a JSON object")
        except ValueError:
            logger.error("Failed to parse content map JSON: %s", raw[:500])
            # Return a minimal fallback
            result = {
                "topics": [],
                "entities": [],
                "key_terms": [],
                "source_coverage": [],
                "gaps": [],
                "summary": "Content map generation failed -- raw LLM output could not be parsed.",
                "_raw_response": raw[:2000],
            }

        safe_set_run(
            extract_run,
            outputs={
                "trimmed_input": trimmed,
                "source_summary_count_final": len(source_summaries),
                "prompt_chars": len(user_prompt),
                "parse_ok": parse_ok,
                "topic_count": _len_if_list(result.get("topics")),
                "entity_count": _len_if_list(result.get("entities")),
                "key_term_count": _len_if_list(result.get("key_terms")),
                "heading_cluster_count": len(heading_clusters),
            },
        )
        return result


def _len_if_list(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0
