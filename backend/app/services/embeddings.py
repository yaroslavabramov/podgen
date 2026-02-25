import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
    Filter,
    FieldCondition,
    FilterSelector,
    MatchAny,
    MatchValue,
)

from app.config import settings
from app.services.llm import get_embeddings, EMBEDDING_DIMENSIONS

_qdrant_client: QdrantClient | None = None


def get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantClient(url=settings.qdrant_url)
    return _qdrant_client


def ensure_collection() -> None:
    client = get_qdrant_client()
    collections = [c.name for c in client.get_collections().collections]
    if settings.qdrant_collection not in collections:
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(size=EMBEDDING_DIMENSIONS, distance=Distance.COSINE),
        )


async def index_chunks(chunks: list[dict]) -> list[uuid.UUID]:
    """Index chunks into Qdrant. Each chunk dict: {id, content, source_id, heading_path, source_type, domain_reliability}"""
    if not chunks:
        return []

    client = get_qdrant_client()
    ensure_collection()

    texts = [c["content"] for c in chunks]
    embeddings = await get_embeddings(texts)

    points = []
    point_ids = []
    for chunk, embedding in zip(chunks, embeddings):
        point_id = uuid.uuid4()
        point_ids.append(point_id)
        points.append(PointStruct(
            id=str(point_id),
            vector=embedding,
            payload={
                "chunk_id": str(chunk["id"]),
                "source_id": str(chunk["source_id"]),
                "heading_path": chunk.get("heading_path", []),
                "source_type": chunk.get("source_type", ""),
                "domain_reliability": chunk.get("domain_reliability", "unknown"),
            },
        ))

    # Batch upsert (max 100 per batch)
    for i in range(0, len(points), 100):
        client.upsert(collection_name=settings.qdrant_collection, points=points[i:i + 100])

    return point_ids


async def vector_search(
    query: str,
    top_k: int = 10,
    source_ids: list[str] | None = None,
) -> list[dict]:
    """Search Qdrant for similar chunks."""
    client = get_qdrant_client()
    ensure_collection()

    query_embedding = (await get_embeddings([query]))[0]

    search_filter = None
    if source_ids:
        search_filter = Filter(
            must=[FieldCondition(key="source_id", match=MatchAny(any=source_ids))]
        )

    response = client.query_points(
        collection_name=settings.qdrant_collection,
        query=query_embedding,
        query_filter=search_filter,
        limit=top_k,
    )

    return [
        {
            "chunk_id": hit.payload["chunk_id"],
            "source_id": hit.payload["source_id"],
            "score": hit.score,
            "heading_path": hit.payload.get("heading_path", []),
        }
        for hit in response.points
    ]


def delete_points_by_source_id(source_id: str) -> None:
    """Delete all vectors in Qdrant associated with a source."""
    client = get_qdrant_client()
    ensure_collection()
    source_filter = Filter(
        must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]
    )
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=FilterSelector(filter=source_filter),
    )
