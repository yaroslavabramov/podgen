from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_OBSERVED
from app.generation_v2.tools.evidence_utils import (
    _confidence,
    _exact_quote_source_span,
    _locator_type_for_label,
    _model_file_read_snippet,
    _model_read_evidence_strength,
    _model_read_locator_type,
    _nearest_locator_for_position,
    _normalize,
    _uuid_list,
)
from app.generation_v2.tools.evidence_window_refs import create_window_ref
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceRetrievalChannel,
    ProviderCorpusFile,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)
from app.services.storage import download_text, get_minio_client


_CANONICAL_TEXT_KINDS = {
    SourceRepresentationKind.PAGE_MARKED_TEXT,
    SourceRepresentationKind.EXTRACTED_TEXT,
    SourceRepresentationKind.TRANSCRIPT,
    SourceRepresentationKind.RENDERED_SNAPSHOT,
    SourceRepresentationKind.FETCHED_HTML,
}


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
        reported_quote = str(read_result.get("quote_text") or "").strip()
        canonical_match = await self._canonical_quote_match(version, reported_quote) if reported_quote else None
        quote_verified = canonical_match is not None
        if reported_quote:
            read_result["quote_verification"] = "canonical_text" if quote_verified else "model_reported_unverified"
            read_result["exact_quote_verified"] = quote_verified
        if reported_quote and not quote_verified:
            read_result["model_reported_quote_text"] = reported_quote
            read_result["quote_text"] = ""
            read_result["snippet_text"] = str(read_result.get("snippet_text") or reported_quote)
            if str(read_result.get("support_level") or "") == "direct":
                read_result["support_level"] = "supported"
        canonical_version = version
        canonical_window_ref = ""
        if canonical_match is not None:
            canonical_version, canonical_text, quote_start, quote_end = canonical_match
            canonical_quote = canonical_text[quote_start:quote_end]
            if canonical_quote != reported_quote:
                read_result["model_reported_quote_text"] = reported_quote
            # Exact evidence must persist the source's bytes/text span, not the
            # model's case- or whitespace-normalized rendering of that span.
            read_result["quote_text"] = canonical_quote
            canonical_window_ref = create_window_ref(
                source_version_id=canonical_version.id,
                text=canonical_text,
                start_offset=quote_start,
                end_offset=quote_end,
            )
            allow_timestamps = canonical_version.kind == SourceRepresentationKind.TRANSCRIPT
            canonical_locator = _nearest_locator_for_position(
                canonical_text,
                quote_start,
                allow_timestamps=allow_timestamps,
            )
            read_result["locator_text"] = canonical_locator or f"source offset {quote_start}"
            read_result["locator_type"] = (
                _locator_type_for_label(canonical_locator, allow_timestamps=allow_timestamps)
                if canonical_locator
                else "section"
            )
        snippet_text = _model_file_read_snippet(read_result)
        attributes = provider_file.attributes or {}
        return await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=version.source_id,
            source_version_id=canonical_version.id,
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
            normalized_quote_text=_normalize(
                str(read_result.get("quote_text") or read_result.get("snippet_text") or snippet_text)
            ),
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
                "model_reported_quote_text": read_result.get("model_reported_quote_text"),
                "quote_verification": read_result.get("quote_verification"),
                "exact_quote_verified": bool(read_result.get("exact_quote_verified")),
                "locator_hint": locator_hint,
                "source_version_kind": canonical_version.kind,
                "canonical_quote_verified": quote_verified,
                **({"window_ref": canonical_window_ref} if canonical_window_ref else {}),
                "model_read_file_id": read_result.get("_model_read_file_id"),
                "vector_store_provider_file_id": provider_file.provider_file_id,
                "provider_corpus_file_id": str(provider_file.id),
                "model_read_cache_key": cache_key,
                "model_read_request_cache_key": request_cache_key,
            },
        )

    async def _quote_exists_in_canonical_text(self, version: SourceVersion, quote: str) -> bool:
        return await EvidenceModelReadPersistenceMixin._canonical_quote_match(self, version, quote) is not None

    async def _canonical_quote_match(
        self,
        version: SourceVersion,
        quote: str,
    ) -> tuple[SourceVersion, str, int, int] | None:
        selected_version_ids = {str(item) for item in getattr(self.run_view, "selected_source_version_ids", []) or []}
        selected_version_uuids = _uuid_list(list(selected_version_ids))
        if not selected_version_uuids:
            return None
        result = await self.db.execute(
            select(SourceVersion).where(
                SourceVersion.id.in_(selected_version_uuids),
                SourceVersion.source_id == version.source_id,
                SourceVersion.status == V2RecordStatus.READY,
                SourceVersion.kind.in_(list(_CANONICAL_TEXT_KINDS)),
            )
        )
        for candidate in result.scalars():
            if str(candidate.id) not in selected_version_ids:
                continue
            storage_path = getattr(candidate, "storage_path", None)
            if not storage_path or getattr(candidate, "kind", None) not in _CANONICAL_TEXT_KINDS:
                continue
            try:
                text = download_text(get_minio_client(), storage_path)
            except Exception:
                continue
            canonical_quote = _exact_quote_source_span(quote, text)
            if canonical_quote is not None:
                start = text.index(canonical_quote)
                return candidate, text, start, start + len(canonical_quote)
        return None


__all__ = ["EvidenceModelReadPersistenceMixin"]
