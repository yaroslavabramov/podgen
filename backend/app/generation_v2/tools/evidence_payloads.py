from __future__ import annotations

from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.tools.evidence_refs import (
    source_card_for_id,
    source_domain_for_id,
    source_ref_for_id,
    source_title_for_id,
)
from app.generation_v2.tools.evidence_tool_payloads import coverage_warning as _coverage_warning
from app.models.generation_v2 import EvidenceItem


class EvidencePayloadMixin:
    async def _evidence_preview_payload(self, item: EvidenceItem, *, max_short_snippet_chars: int = 420) -> dict[str, Any]:
        context = await self._source_ref_context()
        source_ref = source_ref_for_id(str(item.source_id), context)
        replacements = _evidence_replacements(item, source_ref, context=context)
        full_snippet = model_preview_text(
            item.snippet_text,
            replacements=replacements,
        )
        snippet = full_snippet[:max_short_snippet_chars].rstrip()
        full_title = model_preview_text(
            source_title_for_id(str(item.source_id), context),
            replacements=replacements,
        )
        full_domain = model_preview_text(
            source_domain_for_id(str(item.source_id), context),
            replacements=replacements,
        )
        full_locator = model_preview_text(item.locator_text, replacements=replacements)
        payload: dict[str, Any] = {
            "evidence_id": str(item.id),
            "support_status": "preview_only",
            "exact_read_next_action": "read_evidence",
            "source_ref": source_ref,
            "source_title": full_title[:220],
            "source_title_chars": len(full_title),
            "source_title_truncated": len(full_title) > 220,
            "source_domain": full_domain[:180],
            "source_domain_chars": len(full_domain),
            "source_domain_truncated": len(full_domain) > 180,
            "locator": full_locator[:300],
            "locator_chars": len(full_locator),
            "locator_truncated": len(full_locator) > 300,
            "short_snippet": snippet,
            "short_snippet_chars": len(full_snippet),
            "short_snippet_truncated": len(full_snippet) > len(snippet),
            "short_snippet_omitted_chars": max(0, len(full_snippet) - len(snippet)),
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
        source_ref = source_ref_for_id(str(item.source_id), context)
        replacements = _evidence_replacements(item, source_ref, context=context)
        snippet = model_preview_text(
            item.snippet_text,
            replacements=replacements,
            collapse_whitespace=False,
        )
        source_title = model_preview_text(
            source_title_for_id(str(item.source_id), context),
            replacements=replacements,
        )
        source_domain = model_preview_text(
            source_domain_for_id(str(item.source_id), context),
            replacements=replacements,
        )
        locator = model_preview_text(item.locator_text, replacements=replacements)
        if len(snippet) > max_snippet_chars:
            return {
                "evidence_id": str(item.id),
                "source_ref": source_ref,
                "source_title": source_title[:220],
                "source_title_chars": len(source_title),
                "source_title_truncated": len(source_title) > 220,
                "source_domain": source_domain[:180],
                "source_domain_chars": len(source_domain),
                "source_domain_truncated": len(source_domain) > 180,
                "locator_text": locator[:500],
                "locator_chars": len(locator),
                "locator_truncated": len(locator) > 500,
                "locator_type": item.locator_type,
                "error": "evidence_too_large_use_source_window",
                "snippet_chars": len(snippet),
            }
        metadata = item.metadata_ or {}
        payload: dict[str, Any] = {
            "evidence_id": str(item.id),
            "source_ref": source_ref,
            "source_title": source_title[:220],
            "source_title_chars": len(source_title),
            "source_title_truncated": len(source_title) > 220,
            "source_domain": source_domain[:180],
            "source_domain_chars": len(source_domain),
            "source_domain_truncated": len(source_domain) > 180,
            "locator_text": locator[:500],
            "locator_chars": len(locator),
            "locator_truncated": len(locator) > 500,
            "locator_type": item.locator_type,
            "snippet_text": snippet,
            "query_text": model_preview_text(item.query_text, replacements=replacements),
            "purpose": model_preview_text(item.purpose, replacements=replacements),
        }
        if metadata.get("model_file_read"):
            payload.update(
                {
                    "support_assessment": metadata.get("support_level"),
                    "support_summary": model_preview_text(
                        metadata.get("support_summary"), replacements=replacements
                    ),
                    "uncertainty": model_preview_text(metadata.get("uncertainty"), replacements=replacements),
                    "quote_text": model_preview_text(metadata.get("quote_text"), replacements=replacements),
                }
            )
        warning = _coverage_warning(item)
        if warning:
            payload["coverage_warning"] = model_preview_text(warning, replacements=replacements)
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    async def _evidence_detail_page_payload(
        self,
        item: EvidenceItem,
        *,
        start_offset: int,
        end_offset: int,
    ) -> dict[str, Any]:
        """Expose one exact part while leaving the persisted semantic field untouched."""

        raw_snippet = str(item.snippet_text or "")
        if start_offset < 0 or end_offset <= start_offset or end_offset > len(raw_snippet):
            return {"error": "invalid_evidence_page_range"}
        context = await self._source_ref_context()
        source_ref = source_ref_for_id(str(item.source_id), context)
        replacements = _evidence_replacements(item, source_ref, context=context)
        payload = await self._evidence_detail_payload(
            item,
            max_snippet_chars=max(2400, len(raw_snippet) * 2),
        )
        payload["snippet_text"] = model_preview_text(
            raw_snippet[start_offset:end_offset],
            replacements=replacements,
            collapse_whitespace=False,
        )
        payload.update(
            {
                "snippet_start": start_offset,
                "snippet_end": end_offset,
                "snippet_chars": len(raw_snippet),
                "snippet_complete": start_offset == 0 and end_offset == len(raw_snippet),
            }
        )
        payload.pop("error", None)
        return payload


def _evidence_replacements(item: EvidenceItem, source_ref: str, *, context: Any) -> dict[str, str]:
    card = source_card_for_id(str(item.source_id), context)
    return {
        internal_id: source_ref
        for internal_id in [
            str(item.source_id or ""),
            str(item.source_version_id or ""),
            *(str(value) for value in (getattr(card, "source_version_ids", []) or [])),
            *(str(value) for value in (getattr(card, "representation_ids", []) or [])),
            str(getattr(card, "source_profile_id", "") or ""),
        ]
        if internal_id
    }
