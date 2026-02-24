"""ContentMap builder: aggregates topics, entities, and key terms from project chunks."""

import json
import logging
import uuid
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

# Maximum number of chunk summaries to include in a single LLM call
_MAX_CHUNK_SUMMARIES = 200
# Max chars per chunk summary to keep the prompt within context limits
_SUMMARY_CHARS = 200


async def build_content_map(project_id: str, db: AsyncSession) -> str:
    """Build a ContentMap from all project chunks.

    Aggregates topics, entities, and key terms from chunk headings and the
    first 200 characters of each chunk's content. Uses Haiku structured
    output to produce the map.

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
                # --- 1. Gather chunk summaries -------------------------------------
                with trace_span(
                    "ingestion.scout.collect_chunks",
                    run_type="retriever",
                    inputs={"project_id": project_id},
                ) as collect_run:
                    result = await db.execute(
                        select(Chunk, Source.title, Source.source_type)
                        .join(Source, Chunk.source_id == Source.id)
                        .where(Source.project_id == project_id)
                        .order_by(Chunk.chunk_index)
                    )
                    rows = result.all()
                    safe_set_run(collect_run, outputs={"rows_count": len(rows)})

                if not rows:
                    safe_set_run(scout_run, outputs={"status": "no_chunks"})
                    raise ValueError(f"No chunks found for project {project_id}")

                with trace_span(
                    "ingestion.scout.build_chunk_summaries",
                    run_type="chain",
                    inputs={"rows_count": len(rows), "max_rows": _MAX_CHUNK_SUMMARIES},
                ) as summaries_run:
                    chunk_summaries = _build_chunk_summaries(rows)
                    safe_set_run(
                        summaries_run,
                        outputs={
                            "summary_count": len(chunk_summaries),
                            "truncated_input": len(rows) > _MAX_CHUNK_SUMMARIES,
                        },
                    )

                logger.info(
                    "Building content map for project %s with %d chunk summaries",
                    project_id,
                    len(chunk_summaries),
                )

                # --- 2. Call Haiku for structured extraction ------------------------
                with trace_span(
                    "ingestion.scout.extract_map",
                    run_type="chain",
                    inputs={"summary_count": len(chunk_summaries)},
                ) as extract_run:
                    map_json = await _extract_content_map(chunk_summaries)
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
                        "summary_count": len(chunk_summaries),
                    },
                )
                logger.info("Created ContentMap %s for project %s", content_map_id, project_id)
                return content_map_id


def _build_chunk_summaries(rows) -> list[dict]:
    """Build compact chunk summaries for the LLM prompt."""
    summaries: list[dict] = []

    for row in rows[:_MAX_CHUNK_SUMMARIES]:
        chunk: Chunk = row[0]
        source_title: str | None = row[1]
        source_type: str = row[2]

        summary = {
            "source": source_title or "Untitled",
            "type": source_type,
            "headings": chunk.heading_path,
            "preview": chunk.content[:_SUMMARY_CHARS],
        }
        summaries.append(summary)

    return summaries


async def _extract_content_map(chunk_summaries: list[dict]) -> dict:
    """Use Haiku to extract a structured content map from chunk summaries."""
    with trace_span(
        "ingestion.scout.extract_content_map",
        run_type="chain",
        inputs={"summary_count": len(chunk_summaries)},
    ) as extract_run:
        # Truncate summaries to fit context -- serialize and check size
        summaries_json = json.dumps(chunk_summaries, ensure_ascii=False)
        trimmed = False

        # If too large, progressively trim
        if len(summaries_json) > 80_000:
            ratio = 80_000 / len(summaries_json)
            trimmed_count = max(10, int(len(chunk_summaries) * ratio))
            chunk_summaries = chunk_summaries[:trimmed_count]
            summaries_json = json.dumps(chunk_summaries, ensure_ascii=False)
            trimmed = True

        system_prompt = (
            "You are a content analyst. Given chunk summaries from a research project, "
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
            "- topics: Major themes with approximate chunk counts.\n"
            "- entities: People, organizations, technologies, etc.\n"
            "- key_terms: Domain-specific terms with brief context.\n"
            "- source_coverage: Which sources cover which topics.\n"
            "- gaps: Potential information gaps that might need more sources.\n"
            "- summary: A 2-3 sentence overview of the project's content landscape."
        )

        user_prompt = (
            f"Analyze these {len(chunk_summaries)} chunk summaries and produce the content map:\n\n"
            f"{summaries_json}"
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
                "summary_count_final": len(chunk_summaries),
                "prompt_chars": len(user_prompt),
                "parse_ok": parse_ok,
                "topic_count": _len_if_list(result.get("topics")),
                "entity_count": _len_if_list(result.get("entities")),
                "key_term_count": _len_if_list(result.get("key_terms")),
            },
        )
        return result


def _len_if_list(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0
