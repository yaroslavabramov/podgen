"""ContentMap builder: aggregates topics, entities, and key terms from project chunks."""

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.content_map import ContentMap
from app.models.source import Source
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
    # --- 1. Gather chunk summaries ---------------------------------------------
    result = await db.execute(
        select(Chunk, Source.title, Source.source_type)
        .join(Source, Chunk.source_id == Source.id)
        .where(Source.project_id == project_id)
        .order_by(Chunk.chunk_index)
    )
    rows = result.all()

    if not rows:
        raise ValueError(f"No chunks found for project {project_id}")

    chunk_summaries = _build_chunk_summaries(rows)
    logger.info(
        "Building content map for project %s with %d chunk summaries",
        project_id,
        len(chunk_summaries),
    )

    # --- 2. Call Haiku for structured extraction --------------------------------
    map_json = await _extract_content_map(chunk_summaries)

    # --- 3. Persist ContentMap --------------------------------------------------
    content_map = ContentMap(
        id=uuid.uuid4(),
        project_id=uuid.UUID(project_id),
        map_json=map_json,
    )
    db.add(content_map)
    await db.flush()

    content_map_id = str(content_map.id)
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
    # Truncate summaries to fit context -- serialize and check size
    summaries_json = json.dumps(chunk_summaries, ensure_ascii=False)

    # If too large, progressively trim
    if len(summaries_json) > 80_000:
        ratio = 80_000 / len(summaries_json)
        trimmed_count = max(10, int(len(chunk_summaries) * ratio))
        chunk_summaries = chunk_summaries[:trimmed_count]
        summaries_json = json.dumps(chunk_summaries, ensure_ascii=False)

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

    try:
        parsed = parse_llm_json(raw)
        if isinstance(parsed, dict):
            result = parsed
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

    return result
