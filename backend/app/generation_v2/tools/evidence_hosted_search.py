from __future__ import annotations

from app.generation_v2.contracts import V2ToolName
from typing import Any

from sqlalchemy import select

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_OBSERVED
from app.generation_v2.tools.evidence_utils import (
    _confidence,
    _hosted_result_file_id,
    _hosted_result_text,
    _normalize,
    _safe_metadata,
)
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    ProviderCorpusFile,
)


class EvidenceHostedSearchMixin:
    async def persist_hosted_file_search_calls(
        self,
        calls: list[dict[str, Any]],
        response_id: str,
        phase: str,
        agent_name: str,
    ) -> dict[str, Any]:
        observed: list[dict[str, Any]] = []
        for call in calls:
            for result in call.get("results") or []:
                evidence = await self._persist_hosted_file_search_result(
                    result,
                    response_id=response_id,
                    tool_call_id=str(call.get("call_id") or ""),
                    query_text=", ".join(str(item) for item in call.get("queries") or []),
                    result_index=int(result.get("result_index") or 0),
                )
                if evidence:
                    observed.append(await self._evidence_preview_payload(evidence, max_short_snippet_chars=420))
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=phase,
            agent_name=agent_name,
            tool_name=V2ToolName.OPENAI_FILE_SEARCH_OBSERVED,
            input_json={"response_id": response_id, "call_count": len(calls)},
            output_json={
                "observed_evidence_ids": [item["evidence_id"] for item in observed],
                "observed_count": len(observed),
            },
            provider_call_id=response_id,
        )
        return {
            "observed_evidence_ids": [item["evidence_id"] for item in observed],
            "observed_evidence": observed,
            "observed_count": len(observed),
        }

    async def _persist_hosted_file_search_result(
        self,
        result: dict[str, Any],
        *,
        response_id: str,
        tool_call_id: str,
        query_text: str,
        result_index: int,
    ) -> EvidenceItem | None:
        if self.provider_index is None:
            return None
        provider_file_id = _hosted_result_file_id(result)
        if not provider_file_id:
            return None
        file_result = await self.db.execute(
            select(ProviderCorpusFile).where(
                ProviderCorpusFile.provider_index_id == self.provider_index.id,
                ProviderCorpusFile.provider_file_id == provider_file_id,
            )
        )
        provider_file = file_result.scalar_one_or_none()
        if provider_file is None:
            return None
        if not self._in_run_corpus_view(provider_file.source_id, provider_file.source_version_id):
            return None
        eligible_provider_file_ids = {
            str(item) for item in getattr(self.run_view, "eligible_provider_file_ids", []) if item
        }
        if provider_file.provider_file_id not in eligible_provider_file_ids:
            return None
        snippet = _hosted_result_text(result)
        if not snippet.strip():
            return None
        return await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=provider_file.source_id,
            source_version_id=provider_file.source_version_id,
            provider="openai",
            provider_vector_store_id=self.provider_index.provider_vector_store_id if self.provider_index else None,
            provider_file_id=provider_file.provider_file_id,
            provider_vector_store_file_id=provider_file.provider_vector_store_file_id,
            response_id=response_id,
            tool_call_id=tool_call_id,
            retrieval_channel=EvidenceRetrievalChannel.PROVIDER_FILE_SEARCH,
            query_text=query_text,
            purpose="hosted_file_search",
            search_result_index=result_index,
            score=_confidence(result.get("score")),
            snippet_text=snippet,
            normalized_quote_text=_normalize(snippet),
            locator_text=str(result.get("filename") or "file_search snippet"),
            locator_type=EvidenceLocatorType.PROVIDER_SNIPPET,
            evidence_strength=EvidenceStrength.SNIPPET,
            metadata={
                "usage_status": EVIDENCE_STATUS_OBSERVED,
                "hosted_file_search": True,
                "result": _safe_metadata(result),
            },
        )
