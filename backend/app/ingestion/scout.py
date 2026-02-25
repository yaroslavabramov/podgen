"""ContentMap builder: aggregates topics, entities, and key terms from project chunks."""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.content_map import ContentMap
from app.models.source import Source
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope
from app.pipeline.llm_json import parse_llm_json
from app.services.llm import call_haiku_structured

logger = logging.getLogger(__name__)

# Maximum number of source summaries to include in a single LLM call.
_MAX_SOURCE_SUMMARIES = 80
# Max number of sections kept per source summary.
_MAX_SECTIONS_PER_SOURCE = 12
# Max chars for section lead.
_MAX_LEAD_CHARS = 260
_MAX_HEADING_CLUSTERS = 40


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
            }
            by_source[source_id] = source

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
                    "lead": _extract_lead(chunk.content),
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
                "lead": _extract_lead(chunk.content),
                "token_count": 0,
                "chunk_count": 0,
                "chunk_index": int(chunk.chunk_index or 0),
            }
            source["_sections"][section_key] = section
        elif not section.get("lead"):
            section["lead"] = _extract_lead(chunk.content)

        section["token_count"] += token_count
        section["chunk_count"] += 1

    summaries: list[dict] = []
    sorted_sources = sorted(
        by_source.values(),
        key=lambda item: (int(item["total_tokens"]), int(item["chunk_count"])),
        reverse=True,
    )[:_MAX_SOURCE_SUMMARIES]

    for source in sorted_sources:
        if source["_structure_depth"] == 0 and source["_root_sections"]:
            section_values = sorted(source["_root_sections"], key=lambda item: int(item["chunk_index"]))
        else:
            section_values = list(source["_sections"].values())
            section_values.sort(key=lambda item: (int(item["token_count"]), len(item["headings"])), reverse=True)
        section_values = section_values[:_MAX_SECTIONS_PER_SOURCE]

        summaries.append(
            {
                "source_id": source["source_id"],
                "source": source["source"],
                "type": source["type"],
                "url": source["url"],
                "published_at": source["published_at"],
                "total_tokens": source["total_tokens"],
                "chunk_count": source["chunk_count"],
                "section_count": len(source["_sections"]),
                "structure_depth": source["_structure_depth"],
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


def _extract_lead(text: str) -> str:
    """Extract a compact lead sentence from a chunk."""
    if not text:
        return ""

    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""

    # Split on common sentence boundaries first.
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    for part in parts:
        candidate = part.strip()
        if len(candidate) >= 40:
            return candidate[:_MAX_LEAD_CHARS]

    # Fallback: first non-empty fragment.
    for part in parts:
        candidate = part.strip()
        if candidate:
            return candidate[:_MAX_LEAD_CHARS]

    return normalized[:_MAX_LEAD_CHARS]


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
