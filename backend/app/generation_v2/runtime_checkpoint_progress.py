from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.runtime_checkpoint_source_progress import (
    CHECKPOINT_STATE_ID_LIMIT,
    source_inventory_progress,
    source_triage_progress,
)
from app.generation_v2.runtime_checkpoint_trace import checkpoint_source_inventory
from app.models.generation_v2 import RunCorpusView


async def corpus_inventory_state(db: AsyncSession, run_view: RunCorpusView) -> tuple[dict[str, Any], dict[str, str]]:
    from app.generation_v2.tools.evidence_refs import build_source_ref_context
    from app.generation_v2.tools.evidence_source_map import build_source_inventory

    source_ref_by_id: dict[str, str] = {}
    inventory = None
    try:
        inventory = await build_source_inventory(db, run_view)
        source_ref_by_id = build_source_ref_context(inventory).ref_by_source_id
    except Exception:
        inventory = None
        source_ref_by_id = {}
    corpus = {
        "corpus_revision": int(run_view.revision or 1),
        "attempt": int(run_view.attempt or 1),
        "selected_source_count": len(run_view.selected_source_ids or []),
        "selected_source_version_count": len(run_view.selected_source_version_ids or []),
        "selected_source_refs": [
            source_ref_by_id[str(item)]
            for item in (run_view.selected_source_ids or [])[:CHECKPOINT_STATE_ID_LIMIT]
            if str(item) in source_ref_by_id
        ],
    }
    if inventory is not None:
        source_inventory = checkpoint_source_inventory(
            inventory,
            source_ref_by_id=source_ref_by_id,
            limit=80,
        )
        source_total = len(list(getattr(inventory, "source_cards", []) or []))
        source_returned = len(source_inventory)
        corpus["source_inventory"] = source_inventory
        corpus["source_inventory_page"] = {
            "total_count": source_total,
            "returned_count": source_returned,
            "omitted_count": max(0, source_total - source_returned),
            "cursor": "0",
            "next_cursor": str(source_returned) if source_returned < source_total else "",
            "complete": source_returned == source_total,
        }
    return corpus, source_ref_by_id


__all__ = ["corpus_inventory_state", "source_inventory_progress", "source_triage_progress"]
