from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.generation_v2.source_corpus_helpers import provider_external_attributes as _provider_external_attributes
from app.generation_v2.source_corpus_vector_ops import provider_vector_operation_timeout_seconds
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex

logger = logging.getLogger(__name__)


async def sync_provider_file_attributes(
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    attributes: dict[str, str | bool | float],
) -> bool:
    if (
        not settings.openai_api_key
        or not provider_index.provider_vector_store_id
        or not provider_file.provider_file_id
        or _provider_external_attributes(provider_file.attributes or {}) == attributes
    ):
        return True
    try:
        client = _get_openai_client()
        await asyncio.wait_for(
            client.vector_stores.files.update(
                file_id=provider_file.provider_file_id,
                vector_store_id=provider_index.provider_vector_store_id,
                attributes=attributes,
            ),
            timeout=provider_vector_operation_timeout_seconds(),
        )
        return True
    except Exception as exc:
        logger.info("Could not update provider corpus file attributes %s: %s", provider_file.id, exc)
        return False


def _get_openai_client() -> Any:
    from app.generation_v2 import source_corpus

    return source_corpus.get_openai_client()


__all__ = ["sync_provider_file_attributes"]
