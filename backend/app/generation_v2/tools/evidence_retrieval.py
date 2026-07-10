from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from app.generation_v2.artifacts import save_tool_trace
from app.services.llm import get_openai_client
from app.generation_v2.tools.evidence_cache_keys import (
    cache_text as _cache_text,
    json_dumps as json_dumps,
    model_read_cache_key as _model_read_cache_key,
    model_read_request_cache_key as _model_read_request_cache_key,
    run_corpus_snapshot_hash as _run_corpus_snapshot_hash,
    search_cache_key as _search_cache_key,
)
from app.generation_v2.tools.evidence_cache import EvidenceCacheMixin
from app.generation_v2.tools.evidence_local_source import EvidenceLocalSourceMixin
from app.generation_v2.tools.evidence_provider_model_read import EvidenceProviderModelReadMixin
from app.generation_v2.tools.evidence_provider_search import EvidenceProviderSearchMixin
from app.generation_v2.tools.evidence_source_text import source_version_text as _source_version_text
from app.generation_v2.tools.evidence_utils import _model_read_upload_payload


class EvidenceRetrievalMixin(
    EvidenceCacheMixin,
    EvidenceLocalSourceMixin,
    EvidenceProviderModelReadMixin,
    EvidenceProviderSearchMixin,
):
    async def search_corpus(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: list[str],
        limit: int,
        scope: str = "all_sources",
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if len(query) < 3:
            return {"accepted": False, "error": "query must contain at least 3 non-whitespace characters", "results": []}
        limit = max(1, min(limit, 8))
        cache_key = _search_cache_key(self.run_view, query=query, source_ids=source_ids, limit=limit)
        cached_items = await self._cached_evidence_items(cache_field="search_cache_key", cache_key=cache_key, limit=limit)
        if len(cached_items) >= limit:
            payloads = await self._evidence_preview_payloads(cached_items[:limit], max_short_snippet_chars=420)
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase=V2Phase.CORPUS_EVIDENCE,
                agent_name=V2AgentName.CORPUS_EVIDENCE_API,
                tool_name=V2ToolName.SEARCH_CORPUS,
                input_json={"query": query, "purpose": purpose, "source_ids": source_ids, "limit": limit, "cache_key": cache_key},
                output_json={
                    "cache_hit": True,
                    "evidence_ids": [item["evidence_id"] for item in payloads],
                    "result_count": len(payloads),
                },
            )
            return {
                "accepted": True,
                "query": query,
                "scope": scope,
                "returned_count": len(payloads),
                "cache_hit": True,
                "results": payloads,
            }
        provider_items = await self._provider_search(query=query, purpose=purpose, source_ids=source_ids, limit=limit)
        items = provider_items
        if len(items) < limit:
            items.extend(await self._local_fallback_search(query=query, purpose=purpose, source_ids=source_ids, limit=limit - len(items)))
        await self._tag_evidence_cache(
            items[:limit],
            {
                "search_cache_key": cache_key,
                "search_cache_query": _cache_text(query),
                "search_cache_source_ids": sorted(str(item) for item in (source_ids or self.run_view.selected_source_ids)),
                "search_cache_limit": limit,
                "search_cache_corpus_hash": _run_corpus_snapshot_hash(self.run_view),
            },
        )
        payloads = await self._evidence_preview_payloads(items[:limit], max_short_snippet_chars=420)
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=V2Phase.CORPUS_EVIDENCE,
            agent_name=V2AgentName.CORPUS_EVIDENCE_API,
            tool_name=V2ToolName.SEARCH_CORPUS,
            input_json={"query": query, "purpose": purpose, "source_ids": source_ids, "limit": limit, "cache_key": cache_key},
            output_json={"cache_hit": False, "evidence_ids": [item["evidence_id"] for item in payloads], "result_count": len(payloads)},
        )
        return {
            "accepted": True,
            "query": query,
            "scope": scope,
            "returned_count": len(payloads),
            "cache_hit": False,
            "results": payloads,
        }

    def _model_read_cache_key(
        self,
        *,
        source_version_id: str,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
        read_surface_id: str,
    ) -> str:
        return _model_read_cache_key(
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            read_surface_id=read_surface_id,
        )

    def _model_read_request_cache_key(
        self,
        *,
        source_id: str,
        source_version_id: str,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
    ) -> str:
        return _model_read_request_cache_key(
            run_view=self.run_view,
            source_id=source_id,
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
        )

__all__ = [
    "EvidenceRetrievalMixin",
    "_model_read_upload_payload",
    "_source_version_text",
    "get_openai_client",
]
