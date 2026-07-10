from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_utils import _normalize, _retrieval_channel_for_version, _search_text_windows
from app.models.generation_v2 import EvidenceLocatorType, EvidenceStrength


async def search_text_source_windows(owner: Any, version: Any, *, query: str, text: str, limit: int) -> dict[str, Any]:
    windows = _search_text_windows(text, query=query, max_windows=limit, max_chars=1800)
    results = []
    exact_locator_types = {EvidenceLocatorType.PAGE, EvidenceLocatorType.TIMESTAMP, EvidenceLocatorType.SECTION}
    for idx, window in enumerate(windows):
        evidence = await persist_evidence_item(
            owner.db,
            generation_run_id=owner.run.id,
            project_id=owner.run.project_id,
            source_id=version.source_id,
            source_version_id=version.id,
            provider=None,
            retrieval_channel=_retrieval_channel_for_version(version),
            query_text=query,
            purpose="source_internal_search",
            search_result_index=idx,
            score=window["score"],
            snippet_text=window["snippet_text"],
            normalized_quote_text=_normalize(window["snippet_text"][:500]),
            locator_text=window["locator_text"],
            locator_type=window["locator_type"],
            evidence_strength=EvidenceStrength.EXACT
            if window["locator_type"] in exact_locator_types
            else EvidenceStrength.SNIPPET,
            metadata={"source_internal_search": True},
        )
        results.append(await owner._evidence_preview_payload(evidence, max_short_snippet_chars=420))
    await save_tool_trace(
        owner.db,
        generation_run_id=owner.run.id,
        phase=V2Phase.CORPUS_EVIDENCE,
        agent_name=V2AgentName.CORPUS_EVIDENCE_API,
        tool_name=V2ToolName.SEARCH_WITHIN_SOURCE,
        input_json={"source_id": str(version.source_id), "source_version_id": str(version.id), "query": query, "limit": limit},
        output_json={"evidence_ids": [item["evidence_id"] for item in results], "result_count": len(results)},
    )
    return {"results": results}


async def skim_source(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    guard_error = await owner._triage_read_guard_error()
    if guard_error:
        return guard_error
    ref_error = await owner._invalid_source_or_version_ref_error(
        source_ref=str(args.get("source_ref") or ""),
        version_ref=str(args.get("version_ref") or ""),
    )
    if ref_error:
        return {"found": False, **ref_error}
    version = await owner._resolve_source_version(
        source_id=str(args.get("source_id") or ""),
        source_version_id=str(args.get("source_version_id") or ""),
        source_ref=str(args.get("source_ref") or ""),
        version_ref=str(args.get("version_ref") or ""),
    )
    if version is None:
        return {"found": False, "error": "No readable source version in this run corpus view."}
    max_windows = max(1, min(int(args.get("max_windows") or 6), 8))
    windows = await owner._source_windows(version, max_windows=max_windows)
    results = []
    for idx, window in enumerate(windows):
        evidence = await persist_evidence_item(
            owner.db,
            generation_run_id=owner.run.id,
            project_id=owner.run.project_id,
            source_id=version.source_id,
            source_version_id=version.id,
            provider=None,
            retrieval_channel=window["channel"],
            query_text=window["locator_text"],
            purpose="source_skim",
            search_result_index=idx,
            score=float(max_windows - idx),
            snippet_text=window["snippet_text"],
            normalized_quote_text=_normalize(window["snippet_text"][:500]),
            locator_text=window["locator_text"],
            locator_type=window["locator_type"],
            evidence_strength=EvidenceStrength.SNIPPET,
            metadata={"source_skim": True},
        )
        window_payload = {key: value for key, value in window.items() if key != "snippet_text"}
        window_payload["preview_text"] = str(window.get("snippet_text") or "")[:240]
        window_payload["evidence"] = await owner._evidence_preview_payload(evidence, max_short_snippet_chars=420)
        results.append(window_payload)
    await save_tool_trace(
        owner.db,
        generation_run_id=owner.run.id,
        phase=V2Phase.CORPUS_EVIDENCE,
        agent_name=V2AgentName.CORPUS_EVIDENCE_API,
        tool_name=V2ToolName.SKIM_SOURCE,
        input_json={"source_id": str(version.source_id), "source_version_id": str(version.id), "max_windows": max_windows},
        output_json={"evidence_ids": [item["evidence"]["evidence_id"] for item in results], "window_count": len(results)},
    )
    return {
        "found": bool(results),
        "source_ref": await owner._source_ref_for_id(version.source_id),
        "version_ref": await owner._version_ref_for_version(version),
        "outline": [item["locator_text"] for item in results],
        "windows": results,
    }


__all__ = ["search_text_source_windows", "skim_source"]
