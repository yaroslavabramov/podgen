from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.corpus_scope import scope_for_run_view
from app.models.generation_v2 import RunCorpusView, V2ToolTrace


_PAGING_QUALIFIERS = ("section", "set_id", "job_id", "source_ref", "question_id", "requirement_id")


async def durable_paging_progress(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    run_view: RunCorpusView | None,
) -> list[dict[str, Any]]:
    query = select(V2ToolTrace).where(
        V2ToolTrace.generation_run_id == generation_run_id,
        V2ToolTrace.phase == phase,
        V2ToolTrace.agent_name == agent_name,
    )
    if run_view is not None:
        scope = scope_for_run_view(run_view)
        query = query.where(
            V2ToolTrace.corpus_revision == scope.revision,
            V2ToolTrace.attempt == scope.attempt,
        )
    result = await db.execute(query.order_by(V2ToolTrace.created_at.asc(), V2ToolTrace.id.asc()))
    return summarize_paginated_tool_traces(result.scalars())


def summarize_paginated_tool_traces(traces: Iterable[Any]) -> list[dict[str, Any]]:
    """Recover paging position from all accepted pages, not just the last call.

    Numeric cursors are offsets in the V2 model-facing page contracts. Taking
    the union means an accidental later read from cursor 0 cannot erase the
    durable fact that later pages were already read.
    """

    progress_by_key: dict[str, dict[str, Any]] = {}
    covered_offsets: dict[str, set[int]] = defaultdict(set)
    for trace in traces:
        input_json = dict(getattr(trace, "input_json", None) or {})
        output_json = dict(getattr(trace, "output_json", None) or {})
        page = output_json.get("page") if isinstance(output_json.get("page"), dict) else output_json
        if "next_cursor" not in page:
            continue
        if output_json.get("accepted") is False:
            continue
        tool_name = str(getattr(trace, "tool_name", "") or "")
        key, qualifiers = _paging_key(tool_name, input_json)
        cursor = str(page.get("cursor") if page.get("cursor") is not None else input_json.get("cursor") or "0")
        next_cursor = str(page.get("next_cursor") or "")
        returned_count = _returned_count(output_json, page)
        total_count = _total_count(output_json, page, input_json)
        entry = progress_by_key.setdefault(
            key,
            {
                "tool_name": tool_name,
                **qualifiers,
                "call_count": 0,
                "seen_cursors": [],
                "total_count": None,
                "last_reported_next_cursor": "",
                "all_pages_read": False,
            },
        )
        entry["call_count"] += 1
        if cursor not in entry["seen_cursors"]:
            entry["seen_cursors"].append(cursor)
        if total_count is not None:
            entry["total_count"] = total_count
        entry["last_reported_next_cursor"] = next_cursor
        if cursor.isdigit():
            offset = int(cursor)
            covered_offsets[key].update(range(offset, offset + returned_count))

    result: list[dict[str, Any]] = []
    for key, entry in progress_by_key.items():
        total_count = entry["total_count"]
        numeric_cursors = all(str(cursor).isdigit() for cursor in entry["seen_cursors"])
        if numeric_cursors and total_count is not None:
            next_offset = next(
                (offset for offset in range(int(total_count)) if offset not in covered_offsets[key]),
                int(total_count),
            )
            entry["all_pages_read"] = next_offset >= int(total_count)
            entry["next_cursor"] = "" if entry["all_pages_read"] else str(next_offset)
            entry["read_item_count"] = min(len(covered_offsets[key]), int(total_count))
        else:
            entry["next_cursor"] = entry["last_reported_next_cursor"]
            entry["all_pages_read"] = not bool(entry["next_cursor"])
        result.append(entry)
    return result


def _paging_key(tool_name: str, input_json: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    qualifiers = {
        name: str(input_json.get(name) or "")
        for name in _PAGING_QUALIFIERS
        if str(input_json.get(name) or "")
    }
    query_fingerprint = _normalized_query_fingerprint(input_json.get("query"))
    if query_fingerprint:
        qualifiers["query_fingerprint"] = query_fingerprint
    source_refs = [str(item) for item in input_json.get("source_refs") or [] if str(item)]
    if source_refs:
        qualifiers["source_refs"] = source_refs
    key_parts = [tool_name, *(f"{name}={qualifiers[name]}" for name in sorted(qualifiers))]
    return "|".join(key_parts), qualifiers


def _normalized_query_fingerprint(value: Any) -> str:
    normalized = " ".join(str(value or "").casefold().split())
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _returned_count(output_json: dict[str, Any], page: dict[str, Any]) -> int:
    if page.get("returned_count") is not None:
        return max(0, int(page.get("returned_count") or 0))
    if output_json.get("returned_count") is not None:
        return max(0, int(output_json.get("returned_count") or 0))
    for field in ("items", "sources", "source_sets", "capsules", "results"):
        if isinstance(output_json.get(field), list):
            return len(output_json[field])
    return 0


def _total_count(
    output_json: dict[str, Any],
    page: dict[str, Any],
    input_json: dict[str, Any],
) -> int | None:
    requested_source_refs = [str(item) for item in input_json.get("source_refs") or [] if str(item)]
    if requested_source_refs:
        return len(requested_source_refs)
    for value in (
        page.get("total_count"),
        output_json.get("total_count"),
        output_json.get("total"),
        output_json.get("source_count"),
    ):
        if value is not None:
            return max(0, int(value or 0))
    return None


__all__ = ["durable_paging_progress", "summarize_paginated_tool_traces"]
