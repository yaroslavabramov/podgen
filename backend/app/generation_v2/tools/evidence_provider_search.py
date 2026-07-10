from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_utils import _normalize, _provider_filters, _provider_snippet, _uuid_or_none
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    ProviderCorpusFile,
    V2RecordStatus,
)

class EvidenceProviderSearchMixin:
    async def _provider_search(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: list[str],
        limit: int,
    ) -> list[EvidenceItem]:
        if not (
            settings.openai_api_key
            and self.provider_index
            and self.provider_index.provider_vector_store_id
            and self.provider_index.status == V2RecordStatus.READY
        ):
            return []
        from app.generation_v2.tools import evidence_retrieval

        client = evidence_retrieval.get_openai_client()
        filters = _provider_filters(source_ids or [str(item) for item in self.run_view.selected_source_ids])
        results: list[EvidenceItem] = []
        try:
            idx = 0
            async for item in client.vector_stores.search(
                self.provider_index.provider_vector_store_id,
                query=query,
                filters=filters,
                max_num_results=limit,
            ):
                evidence = await self._persist_provider_result(item, query=query, purpose=purpose, result_index=idx)
                if evidence:
                    results.append(evidence)
                idx += 1
        except Exception as exc:
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase=V2Phase.CORPUS_EVIDENCE,
                agent_name=V2AgentName.CORPUS_EVIDENCE_API,
                tool_name=V2ToolName.OPENAI_VECTOR_STORES_SEARCH,
                input_json={"query": query, "source_ids": source_ids, "limit": limit},
                output_json={"error": str(exc)},
            )
        return results

    async def _persist_provider_result(self, item: Any, *, query: str, purpose: str, result_index: int) -> EvidenceItem | None:
        attrs = dict(getattr(item, "attributes", None) or {})
        source_id = _uuid_or_none(attrs.get("source_id"))
        source_version_id = _uuid_or_none(attrs.get("source_version_id"))
        if not source_id or not source_version_id:
            return None
        if not self._in_run_corpus_view(source_id, source_version_id):
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase=V2Phase.CORPUS_EVIDENCE,
                agent_name=V2AgentName.CORPUS_EVIDENCE_API,
                tool_name=V2ToolName.OPENAI_VECTOR_STORES_SEARCH_FILTERED,
                input_json={
                    "source_id": str(source_id),
                    "source_version_id": str(source_version_id),
                    "reason": "outside_run_corpus_view",
                },
                output_json={"accepted": False},
            )
            return None
        snippet = _provider_snippet(item)
        if not snippet.strip():
            return None
        file_result = await self.db.execute(
            select(ProviderCorpusFile).where(
                ProviderCorpusFile.provider_index_id == self.provider_index.id,
                ProviderCorpusFile.source_version_id == source_version_id,
            )
        )
        provider_file = file_result.scalar_one_or_none()
        return await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=source_id,
            source_version_id=source_version_id,
            provider="openai",
            provider_vector_store_id=self.provider_index.provider_vector_store_id,
            provider_file_id=getattr(item, "file_id", None),
            provider_vector_store_file_id=provider_file.provider_vector_store_file_id if provider_file else None,
            retrieval_channel=EvidenceRetrievalChannel.PROVIDER_VECTOR_SEARCH,
            query_text=query,
            purpose=purpose,
            search_result_index=result_index,
            score=float(getattr(item, "score", 0.0) or 0.0),
            snippet_text=snippet,
            normalized_quote_text=_normalize(snippet[:500]),
            locator_text=str(getattr(item, "filename", "") or "provider snippet"),
            locator_type=EvidenceLocatorType.PROVIDER_SNIPPET,
            evidence_strength=EvidenceStrength.SNIPPET,
            metadata={"attributes": attrs, "filename": getattr(item, "filename", None)},
        )
