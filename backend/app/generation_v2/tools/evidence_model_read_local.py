from __future__ import annotations

from typing import Any

from app.generation_v2.tools.evidence_model_read_local_actions import (
    cached_local_model_read_response,
    persist_local_model_read_fallback,
    save_empty_local_model_read_trace,
)
from app.generation_v2.tools.evidence_utils import _retrieval_channel_for_version, _search_text_windows


class EvidenceModelReadLocalMixin:
    async def _model_read_local_fallback(
        self,
        *,
        source_id: str,
        source_version_id: str,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
        request_cache_key: str,
        provider_failures: list[dict[str, Any]],
        provider_candidate_count: int,
    ) -> dict[str, Any]:
        version = await self._resolve_local_fallback_source_version(
            source_id=source_id, source_version_id=source_version_id
        )
        if version is None:
            return {
                "found": False,
                "error": "No provider-backed or locally readable source version is available in this run corpus view.",
                "provider_failure_count": len(provider_failures),
            }
        cache_key = self._model_read_cache_key(
            source_version_id=str(version.id),
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            read_surface_id="local_fallback",
        )
        cached_items = await self._cached_evidence_items(
            cache_field="model_read_cache_key", cache_key=cache_key, limit=1
        )
        if cached_items:
            return await self._cached_local_model_read_response(
                cached_items[0],
                version=version,
                focus_question=focus_question,
                claim_text=claim_text,
                purpose=purpose,
                locator_hint=locator_hint,
                cache_key=cache_key,
                request_cache_key=request_cache_key,
                provider_failures=provider_failures,
            )
        snippet = ""
        locator_text = ""
        locator_type = "unknown"
        channel = _retrieval_channel_for_version(version)
        if locator_hint:
            snippet, locator_text, locator_type, channel = await self._read_source_window(
                version,
                locator=locator_hint,
                max_chars=3000,
            )
        if not snippet.strip():
            from app.generation_v2.tools import evidence_retrieval

            text = evidence_retrieval._source_version_text(version)
            allow_timestamps = await self._source_allows_timestamp_locators(version)
            matches = _search_text_windows(
                text,
                query=claim_text or focus_question,
                max_windows=1,
                max_chars=3000,
                allow_timestamps=allow_timestamps,
            )
            if matches:
                snippet = str(matches[0].get("snippet_text") or "")
                locator_text = str(matches[0].get("locator_text") or "")
                locator_type = str(matches[0].get("locator_type") or "unknown")
        if not snippet.strip():
            await self._save_empty_local_model_read_trace(
                version=version,
                purpose=purpose,
                focus_question=focus_question,
                claim_text=claim_text,
                locator_hint=locator_hint,
                provider_candidate_count=provider_candidate_count,
                provider_failures=provider_failures,
            )
            return {
                "found": False,
                "error": "Provider model-read failed or was unavailable, and local fallback returned no readable window.",
                "provider_failure_count": len(provider_failures),
            }
        return await persist_local_model_read_fallback(
            self,
            version=version,
            snippet=snippet,
            locator_text=locator_text,
            locator_type=locator_type,
            channel=channel,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            cache_key=cache_key,
            request_cache_key=request_cache_key,
            provider_candidate_count=provider_candidate_count,
            provider_failures=provider_failures,
        )

    async def _cached_local_model_read_response(
        self,
        cached: Any,
        *,
        version: Any,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
        cache_key: str,
        request_cache_key: str,
        provider_failures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await cached_local_model_read_response(
            self,
            cached,
            version=version,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            cache_key=cache_key,
            request_cache_key=request_cache_key,
            provider_failures=provider_failures,
        )

    async def _save_empty_local_model_read_trace(
        self,
        *,
        version: Any,
        purpose: str,
        focus_question: str,
        claim_text: str,
        locator_hint: str,
        provider_candidate_count: int,
        provider_failures: list[dict[str, Any]],
    ) -> None:
        await save_empty_local_model_read_trace(
            self,
            version=version,
            purpose=purpose,
            focus_question=focus_question,
            claim_text=claim_text,
            locator_hint=locator_hint,
            provider_candidate_count=provider_candidate_count,
            provider_failures=provider_failures,
        )


__all__ = ["EvidenceModelReadLocalMixin"]
