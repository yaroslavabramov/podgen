from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_run import GenerationRun
from app.models.generation_v2 import ProviderCorpusIndex, RunCorpusView
from app.generation_v2.tools.evidence_retrieval import EvidenceRetrievalMixin
from app.generation_v2.tools.evidence_tool_handlers import EvidenceToolHandlersMixin
from app.generation_v2.tools.evidence_tool_specs import EvidenceToolSpecsMixin


@dataclass
class EvidenceToolset(EvidenceToolSpecsMixin, EvidenceToolHandlersMixin, EvidenceRetrievalMixin):
    db: AsyncSession
    run: GenerationRun
    run_view: RunCorpusView
    provider_index: ProviderCorpusIndex | None
    triage_allowed_source_refs: set[str] | None = None
    triage_current_set_id: str | None = None


__all__ = ["EvidenceToolset"]
