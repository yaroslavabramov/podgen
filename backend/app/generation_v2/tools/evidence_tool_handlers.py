from __future__ import annotations

from typing import Any


from app.generation_v2.tools.evidence_hosted_search import EvidenceHostedSearchMixin
from app.generation_v2.tools.evidence_model_read import EvidenceModelReadMixin
from app.generation_v2.tools.evidence_payloads import EvidencePayloadMixin
from app.generation_v2.tools.evidence_scope import EvidenceScopeMixin
from app.generation_v2.tools.evidence_selection import EvidenceSelectionMixin
from app.generation_v2.tools.evidence_source_tools import EvidenceSourceToolsMixin
from app.generation_v2.tools.evidence_tool_handler_actions import (
    canonical_kinds_by_source_action,
    compare_source_evidence_tool_action,
    find_quotes_tool_action,
    list_sources_tool_action,
    read_evidence_tool_action,
    search_corpus_tool_action,
    search_sources_tool_action,
)
from app.generation_v2.tools.evidence_tool_payloads import compact_source_card as _compact_source_card
from app.generation_v2.tools.evidence_write_actions import EvidenceWriteActionMixin


class EvidenceToolHandlersMixin(
    EvidenceScopeMixin,
    EvidenceSelectionMixin,
    EvidenceWriteActionMixin,
    EvidenceModelReadMixin,
    EvidencePayloadMixin,
    EvidenceHostedSearchMixin,
    EvidenceSourceToolsMixin,
):
    async def list_sources_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await list_sources_tool_action(self, args)

    async def search_corpus_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await search_corpus_tool_action(self, args)

    async def read_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await read_evidence_tool_action(self, args)

    async def find_quotes_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await find_quotes_tool_action(self, args)

    async def search_sources_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await search_sources_tool_action(self, args)

    async def compare_source_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await compare_source_evidence_tool_action(self, args)

    async def compare_evidence_for_question_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        result = await self.compare_source_evidence_tool(args)
        if result.get("accepted"):
            result["expected_next_action"] = (
                "record_research_observation or upsert_editorial_atom for this investigation job before starting another comparison."
            )
        return result

    async def _canonical_kinds_by_source(self) -> dict[str, str]:
        return await canonical_kinds_by_source_action(self)


__all__ = ["EvidenceToolHandlersMixin", "_compact_source_card"]
