from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_research_memo
from app.generation_v2.pipeline.artifact_persistence import (
    save_memo_evidence_gate_artifact,
    save_research_memo_artifacts,
)
from app.generation_v2.pipeline.validation import (
    research_memo_evidence_ids,
    validate_research_memo_evidence,
)
from app.generation_v2.research_workspace import load_research_workspace_artifact
from app.generation_v2.schemas import ResearchAudit, ResearchMemo, ShowBibleContext
from app.generation_v2.source_corpus import list_evidence_dicts
from app.models.generation_run import GenerationRun


@dataclass(frozen=True)
class EditorialMemoPass:
    memo: ResearchMemo
    memo_artifact_id: str
    atom_artifact_ids: list[str]
    memo_evidence_gate: ResearchAudit
    memo_gate_artifact_id: str


async def build_and_validate_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: Any,
    *,
    values: dict[str, Any],
    source_inventory: Any,
    show_bible: ShowBibleContext,
    run_planner: bool,
) -> EditorialMemoPass:
    memo = await build_research_memo(
        db,
        run,
        runtime,
        source_cards=values["source_cards"],
        quote_cards=values["quote_cards"],
        source_map=values["source_map"],
        source_inventory=source_inventory,
        evidence_tools=values["evidence_tools"],
        evidence_toolset=values["evidence_toolset"],
        vector_store_id=values["corpus"].vector_store_id,
        hosted_file_search_handler=values["evidence_toolset"].persist_hosted_file_search_calls,
        show_bible=show_bible,
        run_planner=run_planner,
    )
    research_workspace_artifact = await load_research_workspace_artifact(db, run.id)
    research_workspace_artifact_ids = [str(research_workspace_artifact.id)] if research_workspace_artifact else []
    memo_artifacts = await save_research_memo_artifacts(
        db,
        run,
        runtime,
        memo,
        evidence_ids=research_memo_evidence_ids(memo),
        source_workspace_artifact_ids=values["source_workspace_artifact_ids"],
        research_workspace_artifact_ids=research_workspace_artifact_ids,
        has_vector_store=bool(values["corpus"].vector_store_id),
    )
    memo_evidence_gate = validate_research_memo_evidence(
        memo,
        evidence_records=await list_evidence_dicts(db, run.id),
        run_view=values["corpus"].run_view,
        duration_seconds=int((run.config or {}).get("duration_target", 1800)),
        source_map=values["source_map"],
    )
    memo_gate_artifact_id = await save_memo_evidence_gate_artifact(
        db,
        run,
        memo_evidence_gate,
        memo_artifact_id=memo_artifacts.memo_artifact_id,
        atom_artifact_ids=memo_artifacts.atom_artifact_ids,
        source_map_artifact_id=values["source_map_artifact_id"],
    )
    return EditorialMemoPass(
        memo=memo,
        memo_artifact_id=memo_artifacts.memo_artifact_id,
        atom_artifact_ids=memo_artifacts.atom_artifact_ids,
        memo_evidence_gate=memo_evidence_gate,
        memo_gate_artifact_id=memo_gate_artifact_id,
    )


__all__ = ["EditorialMemoPass", "build_and_validate_research_memo"]
