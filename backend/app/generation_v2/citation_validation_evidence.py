from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.citation_validation_helpers import evidence_to_dict as _evidence_to_dict
from app.generation_v2.corpus_scope import scope_for_run_view
from app.models.generation_v2 import EvidenceItem, RunCorpusView


async def load_evidence_by_id(
    db: AsyncSession | None,
    run_view: RunCorpusView | None,
    evidence_records: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    scope = scope_for_run_view(run_view)
    if evidence_records is not None:
        if run_view is None:
            return {str(item.get("id")): item for item in evidence_records if item.get("id")}
        return {
            str(item.get("id")): item
            for item in evidence_records
            if item.get("id")
            and int(item.get("corpus_revision") or 1) == scope.revision
            and int(item.get("attempt") or 1) == scope.attempt
        }
    if db is None or run_view is None:
        return {}
    result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == run_view.generation_run_id,
            EvidenceItem.corpus_revision == scope.revision,
            EvidenceItem.attempt == scope.attempt,
        )
    )
    return {str(item.id): _evidence_to_dict(item) for item in result.scalars()}


__all__ = ["load_evidence_by_id"]
