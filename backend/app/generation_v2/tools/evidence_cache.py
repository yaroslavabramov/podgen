from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.generation_v2.corpus_scope import scope_for_run_view
from app.models.generation_v2 import EvidenceItem


class EvidenceCacheMixin:
    async def _cached_evidence_items(self, *, cache_field: str, cache_key: str, limit: int) -> list[EvidenceItem]:
        if not cache_key:
            return []
        scope = scope_for_run_view(self.run_view)
        try:
            result = await self.db.execute(
                select(EvidenceItem)
                .where(
                    EvidenceItem.generation_run_id == self.run.id,
                    EvidenceItem.corpus_revision == scope.revision,
                    EvidenceItem.attempt == scope.attempt,
                    EvidenceItem.metadata_[cache_field].as_string() == cache_key,
                )
                .order_by(EvidenceItem.search_result_index.asc(), EvidenceItem.created_at.asc())
                .limit(max(1, limit))
            )
        except Exception:
            return []
        return list(result.scalars())

    async def _tag_evidence_cache(self, items: list[EvidenceItem], metadata: dict[str, Any]) -> None:
        if not items:
            return
        for item in items:
            item.metadata_ = {**(item.metadata_ or {}), **metadata}
        await self.db.flush()


__all__ = ["EvidenceCacheMixin"]
