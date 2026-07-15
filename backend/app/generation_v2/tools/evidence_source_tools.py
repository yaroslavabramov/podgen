from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_source_tool_actions import (
    search_text_source_windows as _search_text_source_windows_action,
    skim_source as _skim_source_action,
)
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_utils import (
    _normalize,
)
from app.generation_v2.tools.evidence_window_refs import WindowRefError, is_window_ref, parse_window_ref
from app.models.generation_v2 import EvidenceLocatorType, EvidenceStrength


def _source_version_text(version: Any) -> str:
    from app.generation_v2.tools import evidence_retrieval

    return evidence_retrieval._source_version_text(version)


class EvidenceSourceToolsMixin:
    async def inspect_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return guard_error
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if ref_error:
            return {"found": False, **ref_error}
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        max_items = max(1, min(int(args.get("max_items") or 24), 40))
        windows = await self._source_windows(version, max_windows=max_items)
        items = [
            {
                "window_ref": item["window_ref"],
                "locator": item["locator_text"],
                "locator_type": item["locator_type"],
                "preview": model_preview_text(item["snippet_text"])[:180],
            }
            for item in windows
        ]
        return {
            "found": bool(items),
            "source_ref": await self._source_ref_for_id(version.source_id),
            "version_ref": await self._version_ref_for_version(version),
            "kind": version.kind,
            "items": items,
        }

    async def read_source_window_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return guard_error
        locator = str(args.get("locator") or "").strip()
        window_ref = str(args.get("window_ref") or "").strip()
        if not window_ref and is_window_ref(locator):
            window_ref = locator
            locator = ""
        parsed_window_ref = None
        if window_ref:
            try:
                parsed_window_ref = parse_window_ref(window_ref)
            except WindowRefError:
                return {"found": False, "error": "invalid_window_ref"}
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if ref_error:
            return {"found": False, **ref_error}
        if not window_ref and not locator:
            return {"found": False, "error": "window_ref_or_locator_required"}
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=(
                str(parsed_window_ref.source_version_id)
                if parsed_window_ref is not None
                else str(args.get("source_version_id") or "")
            ),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        source_ref = str(args.get("source_ref") or "").strip()
        if source_ref:
            resolved_source_id = await self._source_id_from_ref_or_id(source_ref)
            if resolved_source_id and str(version.source_id) != resolved_source_id:
                return {
                    "found": False,
                    "error": "window_ref_source_mismatch" if window_ref else "source_version_mismatch",
                }
        max_chars = max(200, min(int(args.get("max_chars") or 2200), 3000))
        requested_locator = window_ref or locator
        snippet, locator_text, locator_type, channel = await self._read_source_window(
            version,
            locator=requested_locator,
            max_chars=max_chars,
        )
        if not snippet.strip():
            if requested_locator:
                return {
                    "found": False,
                    "error": "window_ref_invalid_or_stale" if window_ref else "locator_not_found",
                    "requested_locator": locator or None,
                }
            return {"found": False, "error": "Source version has no readable local text window."}
        exact_window = bool(window_ref) or (
            bool(locator)
            and locator_type in {EvidenceLocatorType.PAGE, EvidenceLocatorType.TIMESTAMP, EvidenceLocatorType.SECTION}
        )
        evidence = await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=version.source_id,
            source_version_id=version.id,
            provider=None,
            retrieval_channel=channel,
            query_text=locator or None,
            purpose="source_read",
            search_result_index=0,
            score=1.0,
            snippet_text=snippet,
            normalized_quote_text=_normalize(snippet),
            locator_text=locator_text,
            locator_type=locator_type,
            evidence_strength=EvidenceStrength.EXACT if exact_window else EvidenceStrength.SNIPPET,
            metadata={
                "source_read": True,
                "requested_locator": locator,
                **({"window_ref": window_ref} if window_ref else {}),
            },
        )
        payload = await self._evidence_detail_payload(evidence, max_snippet_chars=max_chars)
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=V2Phase.CORPUS_EVIDENCE,
            agent_name=V2AgentName.CORPUS_EVIDENCE_API,
            tool_name=V2ToolName.READ_SOURCE_WINDOW,
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "locator": locator,
                "window_ref": window_ref,
                "max_chars": max_chars,
            },
            output_json={"evidence_id": payload["evidence_id"], "locator_text": payload["locator_text"]},
        )
        return {"found": True, "evidence": payload, **({"window_ref": window_ref} if window_ref else {})}

    async def search_within_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return {**guard_error, "results": []}
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if ref_error:
            return {**ref_error, "results": []}
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"results": [], "error": "No readable source version in this run corpus view."}
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 6), 8))
        text = _source_version_text(version)
        if text.strip():
            return await self._search_text_source_windows(version, query=query, text=text, limit=limit)
        return await self.search_corpus(
            query=query, purpose="source_internal_search", source_ids=[str(version.source_id)], limit=limit
        )

    async def _search_text_source_windows(self, version: Any, *, query: str, text: str, limit: int) -> dict[str, Any]:
        return await _search_text_source_windows_action(self, version, query=query, text=text, limit=limit)

    async def skim_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _skim_source_action(self, args)


__all__ = ["EvidenceSourceToolsMixin"]
