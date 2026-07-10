from __future__ import annotations

import json
from typing import Any


def item_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def to_plain_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    data: dict[str, Any] = {}
    for key in (
        "file_id",
        "filename",
        "score",
        "text",
        "content",
        "attributes",
        "source_id",
        "source_version_id",
    ):
        value = getattr(item, key, None)
        if value is not None:
            data[key] = value
    return data


def function_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if item_get(item, "type") != "function_call":
            continue
        raw_arguments = item_get(item, "arguments", "{}") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {"_raw": raw_arguments}
        calls.append(
            {
                "call_id": item_get(item, "call_id", "") or item_get(item, "id", ""),
                "name": item_get(item, "name", ""),
                "arguments": arguments,
            }
        )
    return calls


def file_search_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if item_get(item, "type") != "file_search_call":
            continue
        raw_results = item_get(item, "results", []) or []
        results = []
        for idx, result in enumerate(raw_results):
            result_dict = to_plain_dict(result)
            result_dict.setdefault("result_index", idx)
            results.append(result_dict)
        calls.append(
            {
                "call_id": item_get(item, "id", "") or item_get(item, "call_id", ""),
                "queries": item_get(item, "queries", []) or [],
                "status": item_get(item, "status", None),
                "results": results,
            }
        )
    return calls


def web_search_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if item_get(item, "type") != "web_search_call":
            continue
        payload = to_plain_dict(item)
        calls.append(
            {
                "call_id": item_get(item, "id", "") or item_get(item, "call_id", ""),
                "status": item_get(item, "status", None),
                "action": payload.get("action"),
                "queries": payload.get("queries") or payload.get("query") or [],
            }
        )
    return calls


def compact_hosted_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for call in calls[:12]:
        compact.append(
            {
                "call_id": call.get("call_id"),
                "status": call.get("status"),
                "queries": call.get("queries"),
                "result_count": len(call.get("results") or []),
            }
        )
    return compact


def hosted_evidence_followup_input(
    summary: dict[str, Any] | None,
    *,
    announced_ids: set[str],
    tools: list[Any],
) -> dict[str, Any] | None:
    if not summary or not has_tool(tools, "select_evidence"):
        return None
    observed = summary.get("observed_evidence") or []
    new_items = [
        {
            "evidence_id": str(item.get("id") or item.get("evidence_id") or ""),
            "source_ref": item.get("source_ref"),
            "source_title": item.get("source_title"),
            "source_domain": item.get("source_domain"),
            "locator": item.get("locator") or item.get("locator_text"),
            "score": item.get("score"),
            "short_snippet": item.get("short_snippet") or str(item.get("snippet_text") or "")[:420],
        }
        for item in observed
        if str(item.get("id") or item.get("evidence_id") or "")
        and str(item.get("id") or item.get("evidence_id") or "") not in announced_ids
    ]
    if not new_items:
        return None
    evidence_ids = [item["evidence_id"] for item in new_items]
    content = (
        "Hosted file_search results were persisted as observed EvidenceItem records. "
        "Treat these as discovery previews. Use evidence_id with select_evidence only after reading enough detail for the source-card, quote, relationship, or claim. "
        "For important PDFs/documents or citation-sensitive claims, call read_source_with_model with the source_ref and an empty version_ref before relying on the preview. "
        "Use source_ref only to choose or relate sources; evidence_id is the handle for evidence selection.\n"
        + json.dumps({"observed_evidence": new_items}, ensure_ascii=False)
    )
    return {
        "evidence_ids": evidence_ids,
        "message": {"role": "user", "content": content},
    }


def has_tool(tools: list[Any], name: str) -> bool:
    return any(tool.name == name for tool in tools)
