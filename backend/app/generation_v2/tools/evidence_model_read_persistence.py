from __future__ import annotations

from typing import Any

from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_OBSERVED
from app.generation_v2.tools.evidence_utils import (
    _confidence,
    _model_file_read_snippet,
    _model_read_evidence_strength,
    _model_read_locator_type,
    _normalize,
)
from app.models.generation_v2 import EvidenceItem, EvidenceRetrievalChannel, ProviderCorpusFile, SourceVersion


class EvidenceModelReadPersistenceMixin:
    async def _persist_model_read_evidence(
        self,
        *,
        provider_file: ProviderCorpusFile,
        version: SourceVersion,
        read_result: dict[str, Any],
        response_id: str | None,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
        cache_key: str,
        request_cache_key: str,
    ) -> EvidenceItem:
        snippet_text = _model_file_read_snippet(read_result)
        attributes = provider_file.attributes or {}
        return await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=version.source_id,
            source_version_id=version.id,
            provider="openai",
            provider_vector_store_id=self.provider_index.provider_vector_store_id if self.provider_index else None,
            provider_file_id=str(read_result.get("_model_read_file_id") or provider_file.provider_file_id),
            provider_vector_store_file_id=provider_file.provider_vector_store_file_id,
            response_id=response_id,
            retrieval_channel=EvidenceRetrievalChannel.MODEL_FILE_READ,
            query_text=focus_question or claim_text,
            purpose=purpose,
            search_result_index=0,
            score=_confidence(read_result.get("confidence")),
            snippet_text=snippet_text,
            normalized_quote_text=_normalize(str(read_result.get("quote_text") or read_result.get("snippet_text") or snippet_text)[:500]),
            locator_text=str(read_result.get("locator_text") or attributes.get("filename") or "model file read"),
            locator_type=_model_read_locator_type(read_result),
            evidence_strength=_model_read_evidence_strength(read_result),
            metadata={
                "usage_status": EVIDENCE_STATUS_OBSERVED,
                "model_file_read": True,
                "support_summary": read_result.get("support_summary"),
                "uncertainty": read_result.get("uncertainty"),
                "supports_query": read_result.get("supports_query"),
                "support_level": read_result.get("support_level"),
                "quote_text": read_result.get("quote_text"),
                "locator_hint": locator_hint,
                "source_version_kind": version.kind,
                "model_read_file_id": read_result.get("_model_read_file_id"),
                "vector_store_provider_file_id": provider_file.provider_file_id,
                "provider_corpus_file_id": str(provider_file.id),
                "model_read_cache_key": cache_key,
                "model_read_request_cache_key": request_cache_key,
            },
        )


__all__ = ["EvidenceModelReadPersistenceMixin"]
