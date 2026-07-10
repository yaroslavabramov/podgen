from __future__ import annotations

from typing import Any

from app.models.generation_v2 import EvidenceItem


def evidence_to_dict(item: EvidenceItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "generation_run_id": str(item.generation_run_id),
        "project_id": str(item.project_id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "provider": item.provider,
        "provider_vector_store_id": item.provider_vector_store_id,
        "provider_file_id": item.provider_file_id,
        "provider_vector_store_file_id": item.provider_vector_store_file_id,
        "response_id": item.response_id,
        "tool_call_id": item.tool_call_id,
        "retrieval_channel": item.retrieval_channel,
        "query_text": item.query_text,
        "purpose": item.purpose,
        "search_result_index": item.search_result_index,
        "score": item.score,
        "snippet_text": item.snippet_text,
        "normalized_quote_text": item.normalized_quote_text,
        "locator_text": item.locator_text,
        "locator_type": item.locator_type,
        "evidence_strength": item.evidence_strength,
        "snippet_hash": item.snippet_hash,
        "metadata": item.metadata_,
    }


__all__ = ["evidence_to_dict"]
