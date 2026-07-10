from __future__ import annotations

from typing import Any

from app.generation_v2.tools.evidence_refs import source_domain_for_id, source_ref_for_id, source_title_for_id
from app.generation_v2.tools.evidence_tool_payloads import coverage_warning as _coverage_warning
from app.models.generation_v2 import EvidenceItem


class EvidencePayloadMixin:
    async def _evidence_preview_payload(self, item: EvidenceItem, *, max_short_snippet_chars: int = 420) -> dict[str, Any]:
        context = await self._source_ref_context()
        snippet = " ".join(str(item.snippet_text or "").split())
        if len(snippet) > max_short_snippet_chars:
            snippet = snippet[:max_short_snippet_chars].rstrip()
        payload: dict[str, Any] = {
            "evidence_id": str(item.id),
            "source_ref": source_ref_for_id(str(item.source_id), context),
            "source_title": source_title_for_id(str(item.source_id), context)[:220],
            "source_domain": source_domain_for_id(str(item.source_id), context),
            "locator": item.locator_text,
            "short_snippet": snippet,
            "score": item.score,
        }
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    async def _evidence_preview_payloads(self, items: list[EvidenceItem], *, max_short_snippet_chars: int = 420) -> list[dict[str, Any]]:
        return [
            await self._evidence_preview_payload(item, max_short_snippet_chars=max_short_snippet_chars)
            for item in items
        ]

    async def _evidence_detail_payload(self, item: EvidenceItem, *, max_snippet_chars: int = 2400) -> dict[str, Any]:
        context = await self._source_ref_context()
        snippet = str(item.snippet_text or "")
        if len(snippet) > max_snippet_chars:
            return {
                "evidence_id": str(item.id),
                "source_ref": source_ref_for_id(str(item.source_id), context),
                "source_title": source_title_for_id(str(item.source_id), context)[:220],
                "source_domain": source_domain_for_id(str(item.source_id), context),
                "locator_text": item.locator_text,
                "locator_type": item.locator_type,
                "error": "evidence_too_large_use_source_window",
                "snippet_chars": len(snippet),
            }
        metadata = item.metadata_ or {}
        payload: dict[str, Any] = {
            "evidence_id": str(item.id),
            "source_ref": source_ref_for_id(str(item.source_id), context),
            "source_title": source_title_for_id(str(item.source_id), context)[:220],
            "source_domain": source_domain_for_id(str(item.source_id), context),
            "locator_text": item.locator_text,
            "locator_type": item.locator_type,
            "snippet_text": snippet,
            "query_text": item.query_text,
            "purpose": item.purpose,
        }
        if metadata.get("model_file_read"):
            payload.update(
                {
                    "support_assessment": metadata.get("support_level"),
                    "support_summary": metadata.get("support_summary"),
                    "uncertainty": metadata.get("uncertainty"),
                    "quote_text": metadata.get("quote_text"),
                }
            )
        warning = _coverage_warning(item)
        if warning:
            payload["coverage_warning"] = warning
        return {key: value for key, value in payload.items() if value not in (None, "", [])}
