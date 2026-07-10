from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.runtime_checkpoint_read_counts import (
    read_counts_by_source_ref as _read_counts_by_source_ref,
    source_refs_from_trace as _source_refs_from_trace,
)
from app.models.generation_v2 import RunCorpusView, V2ToolTrace

CHECKPOINT_STATE_ID_LIMIT = 40


async def source_inventory_progress(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
) -> dict[str, Any] | None:
    list_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == phase,
            V2ToolTrace.agent_name == agent_name,
            V2ToolTrace.tool_name == "list_sources",
        )
        .order_by(V2ToolTrace.created_at.asc())
    )
    seen_cursors: list[str] = []
    seen_source_refs: list[str] = []
    total_source_count = None
    next_cursor = ""
    for trace in list_result.scalars():
        input_json = trace.input_json or {}
        output_json = trace.output_json or {}
        cursor = str(input_json.get("cursor") or "")
        if cursor not in seen_cursors:
            seen_cursors.append(cursor)
        if output_json.get("total") is not None:
            total_source_count = int(output_json.get("total") or 0)
        next_cursor = str(output_json.get("next_cursor") or "")
        for source in output_json.get("sources") or []:
            source_ref = str(source.get("source_ref") or "")
            if source_ref and source_ref not in seen_source_refs:
                seen_source_refs.append(source_ref)
    if not seen_cursors and not seen_source_refs:
        return None
    return {
        "listed_source_count": len(seen_source_refs),
        "total_source_count": total_source_count,
        "seen_cursors": seen_cursors[:CHECKPOINT_STATE_ID_LIMIT],
        "next_cursor": next_cursor,
        "all_sources_listed": bool(total_source_count is not None and len(seen_source_refs) >= total_source_count),
        "listed_source_refs": seen_source_refs[:CHECKPOINT_STATE_ID_LIMIT],
    }


async def source_triage_progress(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    run_view: RunCorpusView,
    source_ref_by_id: dict[str, str],
) -> dict[str, Any]:
    from app.generation_v2.tools.evidence_source_map import build_source_inventory, build_source_map_from_write_actions

    inventory = await build_source_inventory(db, run_view)
    source_map = await build_source_map_from_write_actions(
        db,
        generation_run_id=generation_run_id,
        inventory=inventory,
    )
    mapped_source_refs = [source_ref_by_id.get(card.source_id, card.source_id) for card in source_map.source_cards]
    cluster_source_refs = _cluster_source_refs(source_map, source_ref_by_id)
    relationship_source_refs = _relationship_source_refs(source_map, source_ref_by_id)
    mapped_ref_set = set(mapped_source_refs) | set(cluster_source_refs) | set(relationship_source_refs)
    selected_source_ids = [str(item) for item in run_view.selected_source_ids or []]
    read_counts = await _read_counts_by_source_ref(
        db,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
    )
    read_source_refs = sorted(read_counts)
    return {
        "source_map": {
            "source_card_count": len(source_map.source_cards),
            "quote_card_count": len(source_map.quote_cards),
            "cluster_count": len(source_map.source_triage.source_clusters),
            "relationship_count": len(source_map.source_triage.source_relationships),
            "gap_count": len(source_map.gaps),
            "mapped_source_refs": mapped_source_refs[:CHECKPOINT_STATE_ID_LIMIT],
            "cluster_source_refs": cluster_source_refs[:CHECKPOINT_STATE_ID_LIMIT],
            "relationship_source_refs": relationship_source_refs[:CHECKPOINT_STATE_ID_LIMIT],
            "remaining_source_refs": [
                source_ref_by_id.get(source_id, source_id)
                for source_id in selected_source_ids
                if source_ref_by_id.get(source_id, source_id) not in mapped_ref_set
            ][:CHECKPOINT_STATE_ID_LIMIT],
            "retrieval_notes": source_map.retrieval_notes[:6],
        },
        "source_triage_progress": {
            "read_source_ref_count": len(read_source_refs),
            "read_source_refs": read_source_refs[:CHECKPOINT_STATE_ID_LIMIT],
            "repeated_read_source_refs": [
                {"source_ref": ref, "read_count": count}
                for ref, count in sorted(read_counts.items(), key=lambda item: item[1], reverse=True)
                if count > 1
            ][:12],
            "mapped_or_clustered_source_refs": sorted(mapped_ref_set)[:CHECKPOINT_STATE_ID_LIMIT],
            "read_without_map_write_refs": [ref for ref in read_source_refs if ref not in mapped_ref_set][
                :CHECKPOINT_STATE_ID_LIMIT
            ],
            "workflow_hint": "After reading a small source set, write cards/clusters/relationships/gaps before reading another set.",
        },
    }


def _cluster_source_refs(source_map: Any, source_ref_by_id: dict[str, str]) -> list[str]:
    refs: list[str] = []
    for cluster in source_map.source_triage.source_clusters:
        for source_id in [
            *cluster.source_ids,
            *cluster.primary_source_ids,
            *cluster.secondary_source_ids,
            *cluster.duplicate_source_ids,
        ]:
            source_ref = source_ref_by_id.get(str(source_id), str(source_id))
            if source_ref not in refs:
                refs.append(source_ref)
    return refs


def _relationship_source_refs(source_map: Any, source_ref_by_id: dict[str, str]) -> list[str]:
    refs: list[str] = []
    for relationship in source_map.source_triage.source_relationships:
        for source_id in (relationship.source_id, relationship.related_source_id):
            source_ref = source_ref_by_id.get(str(source_id), str(source_id))
            if source_ref not in refs:
                refs.append(source_ref)
    return refs


__all__ = [
    "CHECKPOINT_STATE_ID_LIMIT",
    "_read_counts_by_source_ref",
    "_source_refs_from_trace",
    "source_inventory_progress",
    "source_triage_progress",
]
