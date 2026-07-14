from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.corpus_scope import scope_for_run_view
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_CITED, EVIDENCE_STATUS_SELECTED
from app.generation_v2.tools.evidence_utils import _dedupe, _unsupported_model_read_selected_as_support, _uuid_or_none
from app.models.generation_v2 import EvidenceItem


class EvidenceSelectionMixin:
    def _evidence_in_current_scope(self, item: EvidenceItem) -> bool:
        scope = scope_for_run_view(self.run_view)
        return bool(
            int(getattr(item, "corpus_revision", 1) or 1) == scope.revision
            and int(getattr(item, "attempt", 1) or 1) == scope.attempt
        )

    async def _mark_evidence_status(
        self,
        evidence_id: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceItem | None:
        if not _uuid_or_none(evidence_id):
            return None
        item = await self.db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != self.run.id or not self._evidence_in_current_scope(item):
            return None
        item.metadata_ = {
            **(item.metadata_ or {}),
            "usage_status": status,
            **(metadata or {}),
        }
        await self.db.flush()
        return item

    async def _evidence_in_run(self, evidence_id: str) -> EvidenceItem | None:
        if not _uuid_or_none(evidence_id):
            return None
        item = await self.db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != self.run.id or not self._evidence_in_current_scope(item):
            return None
        if not self._in_run_corpus_view(item.source_id, item.source_version_id):
            return None
        return item

    def _source_in_run_view(self, source_id: str) -> bool:
        return bool(_uuid_or_none(source_id) and source_id in {str(item) for item in self.run_view.selected_source_ids})

    def _source_version_in_run_view(self, source_version_id: str) -> bool:
        return bool(
            _uuid_or_none(source_version_id)
            and source_version_id in {str(item) for item in self.run_view.selected_source_version_ids}
        )

    async def _selected_evidence(self, evidence_id: str) -> EvidenceItem | None:
        if not _uuid_or_none(evidence_id):
            return None
        item = await self.db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != self.run.id or not self._evidence_in_current_scope(item):
            return None
        if (item.metadata_ or {}).get("usage_status") not in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}:
            return None
        if not self._in_run_corpus_view(item.source_id, item.source_version_id):
            return None
        return item

    async def _selected_evidence_ids(self) -> set[str]:
        scope = scope_for_run_view(self.run_view)
        result = await self.db.execute(
            select(EvidenceItem).where(
                EvidenceItem.generation_run_id == self.run.id,
                EvidenceItem.corpus_revision == scope.revision,
                EvidenceItem.attempt == scope.attempt,
            )
        )
        return {
            str(item.id)
            for item in result.scalars()
            if (item.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
            and self._in_run_corpus_view(item.source_id, item.source_version_id)
        }

    async def _invalid_selected_evidence_ids(self, evidence_ids: list[str], *, source_id: str | None = None) -> list[str]:
        invalid: list[str] = []
        for evidence_id in _dedupe(evidence_ids):
            item = await self._selected_evidence(evidence_id)
            if item is None:
                invalid.append(evidence_id)
                continue
            if source_id and str(item.source_id) != source_id:
                invalid.append(evidence_id)
        return invalid

    async def _select_evidence_ids_for_write(
        self,
        evidence_ids: list[str],
        *,
        source_id: str | None = None,
        purpose: str,
        role: str,
        rationale: str,
    ) -> list[str]:
        invalid: list[str] = []
        for evidence_id in _dedupe(evidence_ids):
            item = await self._evidence_in_run(evidence_id)
            if item is None:
                invalid.append(evidence_id)
                continue
            if source_id and str(item.source_id) != str(source_id):
                invalid.append(evidence_id)
                continue
            if _unsupported_model_read_selected_as_support(item, purpose=purpose, role=role):
                invalid.append(evidence_id)
                continue
            await self._mark_evidence_status(
                evidence_id,
                EVIDENCE_STATUS_SELECTED,
                {
                    "selected_purpose": purpose,
                    "selected_role": role,
                    "selected_rationale": rationale,
                },
            )
        return invalid
