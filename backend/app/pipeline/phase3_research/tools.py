"""Research tools: vector search, keyword search, web search, page fetch."""

import uuid
from urllib.parse import urlparse

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chunk import Chunk
from app.models.source import Source, SourceType, IngestionStatus, DomainReliability
from app.models.evidence import EvidenceSpan
from app.observability import safe_set_run, trace_span
from app.pipeline.step_control import assert_step_not_stopped
from app.services.embeddings import vector_search as qdrant_search


async def vector_search_tool(
    query: str, top_k: int, source_ids: list[str], db: AsyncSession
) -> list[dict]:
    """Vector search in Qdrant, returning chunk info."""
    with trace_span(
        "research.tool.vector_search",
        run_type="retriever",
        inputs={"query_chars": len(query), "top_k": top_k, "source_count": len(source_ids)},
        tags=["research-tool", "vector-search"],
    ) as run_tree:
        results = await qdrant_search(query, top_k=top_k, source_ids=source_ids)

        if not results:
            safe_set_run(run_tree, outputs={"hit_count": 0})
            return []

        chunk_ids_ordered: list[str] = []
        chunk_ids_uuid: list[uuid.UUID] = []
        for hit in results:
            chunk_id_raw = hit.get("chunk_id")
            if not isinstance(chunk_id_raw, str):
                continue
            try:
                chunk_uuid = uuid.UUID(chunk_id_raw)
            except ValueError:
                continue
            chunk_ids_ordered.append(chunk_id_raw)
            chunk_ids_uuid.append(chunk_uuid)

        if not chunk_ids_uuid:
            safe_set_run(run_tree, outputs={"hit_count": 0})
            return []

        chunk_result = await db.execute(select(Chunk).where(Chunk.id.in_(chunk_ids_uuid)))
        chunk_rows = chunk_result.scalars().all()
        chunk_by_id = {str(chunk.id): chunk for chunk in chunk_rows}
        score_by_id = {
            str(hit["chunk_id"]): hit.get("score", 0.0)
            for hit in results
            if isinstance(hit, dict) and isinstance(hit.get("chunk_id"), str)
        }

        chunks = []
        for chunk_id in chunk_ids_ordered:
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            chunks.append(
                {
                    "chunk_id": str(chunk.id),
                    "source_id": str(chunk.source_id),
                    "parent_chunk_id": str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
                    "score": score_by_id.get(chunk_id, 0.0),
                    "preview": chunk.content[:300],
                    "heading_path": chunk.heading_path,
                }
            )
        safe_set_run(run_tree, outputs={"hit_count": len(chunks)})
        return chunks


async def keyword_search_tool(
    query: str, source_ids: list[str], db: AsyncSession, top_k: int = 10
) -> list[dict]:
    """PostgreSQL full-text search on chunks."""
    if not source_ids:
        return []

    with trace_span(
        "research.tool.keyword_search",
        run_type="retriever",
        inputs={"query_chars": len(query), "source_count": len(source_ids)},
        tags=["research-tool", "keyword-search"],
    ) as run_tree:
        sql = text("""
            SELECT c.id, c.source_id, c.parent_chunk_id, c.content, c.heading_path,
                   ts_rank(to_tsvector('simple', c.content), plainto_tsquery('simple', :query)) as rank
            FROM chunks c
            WHERE c.source_id IN :source_ids
              AND to_tsvector('simple', c.content) @@ plainto_tsquery('simple', :query)
            ORDER BY rank DESC
            LIMIT :top_k
        """).bindparams(bindparam("source_ids", expanding=True))

        result = await db.execute(
            sql,
            {"query": query, "top_k": top_k, "source_ids": source_ids},
        )
        rows = result.fetchall()

        payload = [
            {
                "chunk_id": str(row[0]),
                "source_id": str(row[1]),
                "parent_chunk_id": str(row[2]) if row[2] else None,
                "score": float(row[5]),
                "preview": row[3][:300],
                "heading_path": row[4] or [],
            }
            for row in rows
        ]
        safe_set_run(run_tree, outputs={"hit_count": len(payload)})
        return payload


async def get_chunk_content(chunk_id: str, db: AsyncSession) -> dict:
    """Get full chunk content with source metadata."""
    with trace_span(
        "research.tool.get_chunk_content",
        run_type="tool",
        inputs={"chunk_id": chunk_id},
        tags=["research-tool", "chunk-content"],
    ) as run_tree:
        result = await db.execute(
            select(Chunk, Source)
            .join(Source, Chunk.source_id == Source.id)
            .where(Chunk.id == uuid.UUID(chunk_id))
        )
        row = result.first()
        if row is None:
            return {}

        chunk, source = row
        payload = {
            "chunk_id": str(chunk.id),
            "source_id": str(source.id),
            "parent_chunk_id": str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
            "content": chunk.content,
            "heading_path": chunk.heading_path,
            "token_count": chunk.token_count,
            "source_title": source.title,
            "source_type": source.source_type,
            "source_url": source.original_url,
            "domain_reliability": source.domain_reliability,
        }
        safe_set_run(run_tree, outputs={"found": True, "token_count": chunk.token_count})
        return payload


async def web_search_tool(query: str) -> list[dict]:
    """Search web using Tavily API."""
    await assert_step_not_stopped()
    with trace_span(
        "research.tool.web_search",
        run_type="tool",
        inputs={"query_chars": len(query), "provider": "tavily"},
        tags=["research-tool", "web-search"],
    ) as run_tree:
        from tavily import TavilyClient

        client = TavilyClient(api_key=settings.tavily_api_key)
        response = client.search(query, max_results=5, search_depth="advanced")
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:500],
                "score": r.get("score", 0),
            }
            for r in response.get("results", [])
        ]
        safe_set_run(run_tree, outputs={"result_count": len(results)})
        return results


async def fetch_page_tool(url: str) -> dict:
    """Fetch page content using Jina Reader API."""
    import httpx
    from app.ingestion.web import _check_ssrf

    await assert_step_not_stopped()
    domain = urlparse(url).netloc
    with trace_span(
        "research.tool.fetch_page",
        run_type="tool",
        inputs={"url": url, "domain": domain},
        metadata={"provider": "jina-reader"},
        tags=["research-tool", "fetch-page"],
    ) as run_tree:
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
        payload = {
            "title": "",
            "content_markdown": content[:50000],
            "url": url,
        }
        safe_set_run(run_tree, outputs={"content_chars": len(payload["content_markdown"])})
        return payload


async def create_web_evidence(
    url: str, quote: str, asset_type: str, project_id: str, db: AsyncSession,
    generation_run_id: str | None = None,
) -> str:
    """Create a temporary web source + evidence span."""
    await assert_step_not_stopped()
    with trace_span(
        "research.tool.create_web_evidence",
        run_type="tool",
        inputs={
            "url": url,
            "asset_type": asset_type,
            "quote_chars": len(quote),
            "project_id": project_id,
        },
        tags=["research-tool", "create-evidence"],
    ) as run_tree:
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

        evidence_id = str(evidence.id)
        safe_set_run(run_tree, outputs={"evidence_id": evidence_id, "source_id": str(source.id)})
        return evidence_id
