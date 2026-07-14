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
        "run_corpus_view_id": str(run_view.id),
        "corpus_revision": int(run_view.revision or 1),
        "attempt": int(run_view.attempt or 1),
        "selected_source_count": len(run_view.selected_source_ids or []),
        "selected_source_version_count": len(run_view.selected_source_version_ids or []),
        "manifest_hash": run_view.manifest_hash,
        "selected_source_refs": [
            source_ref_by_id.get(str(item), str(item))
            for item in (run_view.selected_source_ids or [])[:CHECKPOINT_STATE_ID_LIMIT]
        ],
    }
    if inventory is not None:
        corpus["source_inventory"] = checkpoint_source_inventory(
            inventory,
            source_ref_by_id=source_ref_by_id,
            limit=80,
        )
    return corpus, source_ref_by_id


__all__ = ["corpus_inventory_state", "source_inventory_progress", "source_triage_progress"]
