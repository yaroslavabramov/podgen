from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_v2 import (
    RunCorpusView,
)
from app.models.source import Source
from app.generation_v2.tools.evidence_source_inventory import build_source_inventory
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref, source_ref_for_id
from app.generation_v2.tools.evidence_source_map_action_helpers import relationship_kind as _relationship_kind
from app.generation_v2.tools.evidence_source_map_actions import build_source_map_from_write_actions
from app.generation_v2.tools.evidence_source_map_gate import (
    missing_source_card_write_sources as _missing_source_card_write_sources,
    validate_source_map_gate,
)
from app.generation_v2.tools.evidence_utils import (
    _uuid_or_none,
)


async def _source_summaries(
    db: AsyncSession,
    run_view: RunCorpusView,
    evidence: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    inventory = await build_source_inventory(db, run_view)
    source_ref_context = build_source_ref_context(inventory)
    evidence_by_source: dict[str, list[dict[str, Any]]] = {}
    ordered_source_ids: list[str] = []
    for item in evidence:
        source_ref = str(item.get("source_ref") or "")
        source_id = resolve_source_ref(source_ref, source_ref_context) if source_ref else str(item.get("source_id") or "")
        if not _uuid_or_none(source_id):
            continue
        evidence_by_source.setdefault(str(source_id), []).append(item)
        if source_id not in ordered_source_ids:
            ordered_source_ids.append(source_id)
    ordered_source_ids = ordered_source_ids[: max(1, min(int(limit or 8), 8))]
    if not ordered_source_ids:
        return []
    sources_result = await db.execute(select(Source).where(Source.id.in_([uuid.UUID(source_id) for source_id in ordered_source_ids])))
    sources_by_id = {str(source.id): source for source in sources_result.scalars()}
    return [
        {
            "source_ref": source_ref_for_id(str(source.id), source_ref_context),
            "title": source.title or source.original_url or source.file_path or "Untitled source",
            "source_type": source.source_type,
            "matched_evidence_count": len(evidence_by_source.get(str(source.id), [])),
            "evidence_ids": [
                str(item.get("evidence_id") or item.get("id") or "")
                for item in evidence_by_source.get(str(source.id), [])[:6]
                if item.get("evidence_id") or item.get("id")
            ],
        }
        for source_id in ordered_source_ids
        if (source := sources_by_id.get(source_id)) is not None
    ]


__all__ = [
    "_missing_source_card_write_sources",
    "_relationship_kind",
    "_source_summaries",
    "build_source_map_from_write_actions",
    "validate_source_map_gate",
]
