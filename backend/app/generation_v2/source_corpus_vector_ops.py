from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex


def provider_vector_operation_timeout_seconds() -> int:
    return max(10, min(60, int(settings.generation_v2_provider_upload_timeout_seconds)))


async def retrieve_vector_store_file(client: Any, provider_index: ProviderCorpusIndex, provider_file_id: str) -> Any | None:
    if not provider_index.provider_vector_store_id or not provider_file_id:
        return None
    try:
        return await asyncio.wait_for(
            client.vector_stores.files.retrieve(
                file_id=provider_file_id,
                vector_store_id=provider_index.provider_vector_store_id,
            ),
            timeout=provider_vector_operation_timeout_seconds(),
        )
    except Exception:
        return None


async def ensure_vector_store_file_attached(
    client: Any,
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    attributes: dict[str, str | bool | float],
) -> Any | None:
    if not provider_index.provider_vector_store_id or not provider_file.provider_file_id:
        return None
    existing = await retrieve_vector_store_file(client, provider_index, provider_file.provider_file_id)
    if existing is not None:
        return existing
    try:
        return await asyncio.wait_for(
            client.vector_stores.files.create(
                vector_store_id=provider_index.provider_vector_store_id,
                file_id=provider_file.provider_file_id,
                attributes=attributes,
            ),
            timeout=provider_vector_operation_timeout_seconds(),
        )
    except Exception:
        existing = await retrieve_vector_store_file(client, provider_index, provider_file.provider_file_id)
        if existing is not None:
            return existing
        raise


async def detach_openai_vector_store_file(client: Any, provider_index: ProviderCorpusIndex, provider_file_id: str) -> None:
    if not settings.openai_api_key or not provider_index.provider_vector_store_id or not provider_file_id:
        return
    await asyncio.wait_for(
        client.vector_stores.files.delete(
            vector_store_id=provider_index.provider_vector_store_id,
            file_id=provider_file_id,
        ),
        timeout=provider_vector_operation_timeout_seconds(),
    )


async def list_vector_store_files(client: Any, provider_index: ProviderCorpusIndex) -> list[Any]:
    if not settings.openai_api_key or not provider_index.provider_vector_store_id:
        return []

    async def _collect() -> list[Any]:
        listing = client.vector_stores.files.list(
            vector_store_id=provider_index.provider_vector_store_id,
            limit=100,
        )
        if hasattr(listing, "__await__"):
            listing = await listing
        if hasattr(listing, "__aiter__"):
            items: list[Any] = []
            async for item in listing:
                items.append(item)
            return items
        if hasattr(listing, "data"):
            return list(listing.data)
        return list(listing)

    return await asyncio.wait_for(_collect(), timeout=provider_vector_operation_timeout_seconds())
