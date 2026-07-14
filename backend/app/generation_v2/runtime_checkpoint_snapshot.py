from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.contracts.agents import V2AgentName
from app.generation_v2.corpus_scope import scope_for_run_view
from app.generation_v2.runtime_checkpoint_progress import (
    corpus_inventory_state,
    source_inventory_progress,
    source_triage_progress,
)
from app.models.generation_v2 import EvidenceItem, RunCorpusView, V2ToolTrace

CHECKPOINT_STATE_ID_LIMIT = 40


async def build_agent_checkpoint_state(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
) -> dict[str, Any]:
    state: dict[str, Any] = {"phase": phase, "agent_name": agent_name}
    run_view, source_ref_by_id = await _add_corpus_state(db, state, generation_run_id=generation_run_id)
    await _add_evidence_state(db, state, generation_run_id=generation_run_id, run_view=run_view)
    await _add_tool_call_counts(
        db,
        state,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
        run_view=run_view,
    )
    await _add_source_inventory_progress(
        db,
        state,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
        run_view=run_view,
    )
    if (
        agent_name == V2AgentName.SOURCE_TRIAGE
        or agent_name.startswith("source_triage_")
    ) and run_view is not None:
        await _add_source_triage_progress(
            db,
            state,
            generation_run_id=generation_run_id,
            phase=phase,
            agent_name=agent_name,
            run_view=run_view,
            source_ref_by_id=source_ref_by_id,
        )
    if phase == "v2_source_triage":
        await _add_source_triage_workspace_state(db, state, generation_run_id=generation_run_id)
    if phase == "v2_editorial_research":
        await _add_research_workspace_state(db, state, generation_run_id=generation_run_id)
    return state


async def _add_evidence_state(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    generation_run_id: uuid.UUID,
    run_view: RunCorpusView | None,
) -> None:
    try:
        query = select(EvidenceItem).where(EvidenceItem.generation_run_id == generation_run_id)
        if run_view is not None:
            scope = scope_for_run_view(run_view)
            query = query.where(
                EvidenceItem.corpus_revision == scope.revision,
                EvidenceItem.attempt == scope.attempt,
            )
        evidence_result = await db.execute(query)
        evidence_items = list(evidence_result.scalars())
        selected = [
            item
            for item in evidence_items
            if (item.metadata_ or {}).get("usage_status") in {"selected", "cited"}
        ]
        state["evidence"] = {
            "observed_count": len(evidence_items),
            "selected_count": len(selected),
            "selected_evidence_ids": [str(item.id) for item in selected[:CHECKPOINT_STATE_ID_LIMIT]],
            "covered_source_ids": sorted({str(item.source_id) for item in selected})[:CHECKPOINT_STATE_ID_LIMIT],
            "selected_evidence_id_omitted_count": max(0, len(selected) - CHECKPOINT_STATE_ID_LIMIT),
            "covered_source_id_count": len({str(item.source_id) for item in selected}),
        }
    except Exception as exc:
        state["evidence_error"] = str(exc)


async def _add_corpus_state(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    generation_run_id: uuid.UUID,
) -> tuple[RunCorpusView | None, dict[str, str]]:
    run_view: RunCorpusView | None = None
    source_ref_by_id: dict[str, str] = {}
    try:
        run_view_result = await db.execute(
            select(RunCorpusView)
            .where(
                RunCorpusView.generation_run_id == generation_run_id,
                RunCorpusView.is_current.is_(True),
            )
            .order_by(RunCorpusView.revision.desc(), RunCorpusView.attempt.desc())
            .limit(1)
        )
        run_view = run_view_result.scalar_one_or_none()
        if run_view:
            state["corpus"], source_ref_by_id = await corpus_inventory_state(db, run_view)
    except Exception as exc:
        state["corpus_error"] = str(exc)
    return run_view, source_ref_by_id


async def _add_tool_call_counts(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    run_view: RunCorpusView | None,
) -> None:
    try:
        query = select(V2ToolTrace.tool_name, func.count(V2ToolTrace.id)).where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == phase,
            V2ToolTrace.agent_name == agent_name,
        )
        if run_view is not None:
            scope = scope_for_run_view(run_view)
            query = query.where(
                V2ToolTrace.corpus_revision == scope.revision,
                V2ToolTrace.attempt == scope.attempt,
            )
        counts_result = await db.execute(query.group_by(V2ToolTrace.tool_name))
        state["tool_call_counts"] = {str(name): int(count) for name, count in counts_result.all()}
    except Exception as exc:
        state["tool_count_error"] = str(exc)


async def _add_source_inventory_progress(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    run_view: RunCorpusView | None,
) -> None:
    try:
        progress = await source_inventory_progress(
            db,
            generation_run_id=generation_run_id,
            phase=phase,
            agent_name=agent_name,
            run_view=run_view,
        )
        if progress:
            state["source_inventory_progress"] = progress
    except Exception as exc:
        state["source_inventory_progress_error"] = str(exc)


async def _add_source_triage_progress(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    run_view: RunCorpusView,
    source_ref_by_id: dict[str, str],
) -> None:
    try:
        state.update(
            await source_triage_progress(
                db,
                generation_run_id=generation_run_id,
                phase=phase,
                agent_name=agent_name,
                run_view=run_view,
                source_ref_by_id=source_ref_by_id,
            )
        )
    except Exception as exc:
        state["source_map_error"] = str(exc)


async def _add_research_workspace_state(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    generation_run_id: uuid.UUID,
) -> None:
    try:
        from app.generation_v2.research_workspace import load_research_workspace, research_workspace_summary_for_prompt
        from app.generation_v2.research_workspace_prompt_payloads import current_job_prompt_payload

        workspace = await load_research_workspace(db, generation_run_id)
        if workspace:
            summary = research_workspace_summary_for_prompt(
                workspace,
                max_jobs=40,
                max_atoms=40,
                include_atom_evidence_ids=False,
                include_gap_evidence_ids=False,
            )
            current_job_id = str(((workspace.get("current_job") or {}).get("job_id")) or "")
            current_job = next(
                (
                    item
                    for item in workspace.get("investigation_jobs") or []
                    if str(item.get("job_id") or "") == current_job_id
                ),
                None,
            )
            if isinstance(current_job, dict):
                summary["current_job_detail"] = current_job_prompt_payload(current_job)
            state["research_workspace"] = summary
    except Exception as exc:
        state["research_workspace_error"] = str(exc)


async def _add_source_triage_workspace_state(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    generation_run_id: uuid.UUID,
) -> None:
    try:
        from app.generation_v2.source_triage_workspace import load_source_triage_workspace
        from app.generation_v2.source_triage_workspace_prompting import checkpoint_workspace_payload

        workspace = await load_source_triage_workspace(db, generation_run_id)
        if workspace:
            state["source_triage_workspace"] = checkpoint_workspace_payload(workspace, max_sets=40)
    except Exception as exc:
        state["source_triage_workspace_error"] = str(exc)


__all__ = ["build_agent_checkpoint_state"]
