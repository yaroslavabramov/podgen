from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_v2 import V2ToolTrace
from app.generation_v2.corpus_scope import scope_for_run_view

CHECKPOINT_READ_TOOL_NAMES = (
    "skim_source",
    "read_source_window",
    "inspect_source",
    "compare_source_evidence",
    "search_corpus",
    "read_evidence",
)


async def read_counts_by_source_ref(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    run_view=None,
) -> dict[str, int]:
    query = select(V2ToolTrace).where(
        V2ToolTrace.generation_run_id == generation_run_id,
        V2ToolTrace.phase == phase,
        V2ToolTrace.agent_name == agent_name,
        V2ToolTrace.tool_name.in_(CHECKPOINT_READ_TOOL_NAMES),
    )
    if run_view is not None:
        scope = scope_for_run_view(run_view)
        query = query.where(
            V2ToolTrace.corpus_revision == scope.revision,
            V2ToolTrace.attempt == scope.attempt,
        )
    read_result = await db.execute(query.order_by(V2ToolTrace.created_at.asc()))
    read_counts: dict[str, int] = {}
    for trace in read_result.scalars():
        for ref in source_refs_from_trace(trace):
            read_counts[ref] = read_counts.get(ref, 0) + 1
    return read_counts


def source_refs_from_trace(trace: V2ToolTrace) -> list[str]:
    input_json = trace.input_json or {}
    output_json = trace.output_json or {}
    refs: list[str] = []
    for key in ("source_ref", "related_source_ref"):
        value = input_json.get(key) or output_json.get(key)
        if isinstance(value, str) and value:
            refs.append(value)
    for key in ("source_refs", "related_source_refs"):
        values = input_json.get(key) or output_json.get(key) or []
        if isinstance(values, list):
            refs.extend(str(value) for value in values if value)
    if trace.tool_name == "compare_source_evidence":
        for comparison in output_json.get("comparisons") or []:
            value = comparison.get("source_ref") if isinstance(comparison, dict) else None
            if isinstance(value, str) and value:
                refs.append(value)
    return refs


_read_counts_by_source_ref = read_counts_by_source_ref
_source_refs_from_trace = source_refs_from_trace

__all__ = [
    "CHECKPOINT_READ_TOOL_NAMES",
    "_read_counts_by_source_ref",
    "_source_refs_from_trace",
    "read_counts_by_source_ref",
    "source_refs_from_trace",
]
