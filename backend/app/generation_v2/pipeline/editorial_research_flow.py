from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.pipeline.editorial_research_stage import (
    EditorialResearchStageRunner,
    enqueue_treatment_research_repair_jobs,
)
from app.generation_v2.pipeline.editorial_research_state import (
    EditorialResearchResult,
    editorial_runtime_values,
)
from app.generation_v2.schemas import ShowBibleContext
from app.models.generation_run import GenerationRun


def _runtime_values(triage_state: Any) -> dict[str, Any]:
    return editorial_runtime_values(triage_state)


async def run_editorial_research_until_treatment(
    db: AsyncSession,
    run: GenerationRun,
    runtime: Any,
    *,
    triage_state: Any,
    acquisition_rounds_used: int,
    show_bible: ShowBibleContext,
) -> EditorialResearchResult:
    return await EditorialResearchStageRunner(
        db,
        run,
        runtime,
        triage_state=triage_state,
        acquisition_rounds_used=acquisition_rounds_used,
        show_bible=show_bible,
    ).run_stage()


__all__ = [
    "_runtime_values",
    "enqueue_treatment_research_repair_jobs",
    "run_editorial_research_until_treatment",
]
