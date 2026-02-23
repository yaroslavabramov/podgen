"""Research tools: vector search, keyword search, web search, page fetch."""

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chunk import Chunk
from app.models.source import Source, SourceType, IngestionStatus, DomainReliability
from app.models.evidence import EvidenceSpan
from app.services.embeddings import vector_search as qdrant_search


async def vector_search_tool(
    query: str, top_k: int, source_ids: list[str], db: AsyncSession
) -> list[dict]:
    """Vector search in Qdrant, returning chunk info."""
    results = await qdrant_search(query, top_k=top_k, source_ids=source_ids)

    chunks = []
    for hit in results:
        result = await db.execute(select(Chunk).where(Chunk.id == uuid.UUID(hit["chunk_id"])))
        chunk = result.scalar_one_or_none()
        if chunk:
            chunks.append({
                "chunk_id": str(chunk.id),
                "source_id": str(chunk.source_id),
                "score": hit["score"],
                "preview": chunk.content[:300],
                "heading_path": chunk.heading_path,
            })
    return chunks


async def keyword_search_tool(
    query: str, source_ids: list[str], db: AsyncSession
) -> list[dict]:
    """PostgreSQL full-text search on chunks."""
    source_filter = ", ".join(f"'{s}'" for s in source_ids)
    if not source_filter:
        return []

    sql = text(f"""
        SELECT c.id, c.source_id, c.content, c.heading_path,
               ts_rank(to_tsvector('english', c.content), plainto_tsquery('english', :query)) as rank
        FROM chunks c
        WHERE c.source_id IN ({source_filter})
          AND to_tsvector('english', c.content) @@ plainto_tsquery('english', :query)
        ORDER BY rank DESC
        LIMIT 10
    """)

    result = await db.execute(sql, {"query": query})
    rows = result.fetchall()

    return [
        {
            "chunk_id": str(row[0]),
            "source_id": str(row[1]),
            "score": float(row[4]),
            "preview": row[2][:300],
            "heading_path": row[3] or [],
        }
        for row in rows
    ]


async def get_chunk_content(chunk_id: str, db: AsyncSession) -> dict:
    """Get full chunk content with source metadata."""
    result = await db.execute(
        select(Chunk, Source)
        .join(Source, Chunk.source_id == Source.id)
        .where(Chunk.id == uuid.UUID(chunk_id))
    )
    row = result.first()
    if row is None:
        return {}

    chunk, source = row
    return {
        "chunk_id": str(chunk.id),
        "content": chunk.content,
        "heading_path": chunk.heading_path,
        "token_count": chunk.token_count,
        "source_title": source.title,
        "source_type": source.source_type,
        "source_url": source.original_url,
        "domain_reliability": source.domain_reliability,
    }


async def web_search_tool(query: str) -> list[dict]:
    """Search web using Tavily API."""
    from tavily import TavilyClient
    client = TavilyClient(api_key=settings.tavily_api_key)
    response = client.search(query, max_results=5, search_depth="advanced")
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", "")[:500],
            "score": r.get("score", 0),
        }
        for r in response.get("results", [])
    ]


async def fetch_page_tool(url: str) -> dict:
    """Fetch page content using Jina Reader API."""
    import httpx
    from app.ingestion.web import _check_ssrf

    try:
        _check_ssrf(url)
    except ValueError as e:
        return {"error": str(e)}

    headers = {"Accept": "text/markdown"}
    if settings.jina_api_key:
        headers["Authorization"] = f"Bearer {settings.jina_api_key}"

    async with httpx.AsyncClient(timeout=settings.web_fetch_timeout) as client:
        response = await client.get(f"https://r.jina.ai/{url}", headers=headers)
        response.raise_for_status()

    content = response.text
    return {
        "title": "",
        "content_markdown": content[:50000],
        "url": url,
    }


async def create_web_evidence(
    url: str, quote: str, asset_type: str, project_id: str, db: AsyncSession,
    generation_run_id: str | None = None,
) -> str:
    """Create a temporary web source + evidence span."""
    source = Source(
        id=uuid.uuid4(),
        project_id=uuid.UUID(project_id),
        source_type=SourceType.WEB,
        original_url=url,
        ingestion_status=IngestionStatus.COMPLETED,
        is_temporary=True,
        fully_ingested=False,
        domain_reliability=DomainReliability.UNKNOWN,
        generation_run_id=uuid.UUID(generation_run_id) if generation_run_id else None,
    )
    db.add(source)
    await db.flush()

    evidence = EvidenceSpan(
        id=uuid.uuid4(),
        project_id=uuid.UUID(project_id),
        source_id=source.id,
        verbatim_quote=quote,
        asset_type=asset_type,
        narrative_value_score=0.5,
    )
    db.add(evidence)
    await db.flush()

    return str(evidence.id)
