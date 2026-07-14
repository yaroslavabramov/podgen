from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.fallbacks import allow_runtime_fallback as _allow_runtime_fallback
from app.generation_v2.agents.fallbacks import fallback_research_memo as _fallback_research_memo
from app.generation_v2.agents.research_agents import (
    build_research_finalizer_agent,
    build_research_job_agent,
    build_research_planner_agent,
    noop_research_workspace_tools as _noop_research_workspace_tools,
)
from app.generation_v2.research_workspace import (
    build_research_workspace_tools,
    ensure_research_workspace,
    load_research_workspace,
    prepare_research_jobs_for_resume,
    research_finalizer_payload_for_prompt,
    research_job_prompt_payload,
    research_memo_from_workspace,
    research_workspace_summary_for_prompt,
    save_research_workspace,
    source_map_research_summary,
    start_research_job,
    unprocessed_research_jobs,
)
from app.generation_v2.research_workspace_repair_budget import (
    TARGETED_REPAIR_BUDGET,
    complete_repair_operation_in_workspace,
    pending_repair_operation,
    start_repair_operation_in_workspace,
)
from app.generation_v2.runtime import AgentTool, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import QuoteCard, ResearchMemo, ShowBibleContext, SourceCard, SourceMap
from app.generation_v2.tools.evidence_toolset import EvidenceToolset
from app.models.generation_run import GenerationRun


async def build_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    source_map: SourceMap | None = None,
    source_inventory: SourceMap | None = None,
    evidence_tools: list[AgentTool] | None = None,
    evidence_toolset: EvidenceToolset | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
    hosted_file_search_handler=None,
    run_planner: bool = True,
) -> ResearchMemo:
    source_map = source_map or SourceMap(source_cards=source_cards, quote_cards=quote_cards)
    full_source_inventory = source_inventory or source_map
    if db is None:
        return await build_research_memo_ephemeral(
            run,
            runtime,
            source_map=source_map,
            source_inventory=full_source_inventory,
            evidence_tools=evidence_tools,
            show_bible=show_bible,
        )
    workspace = await ensure_research_workspace(
        db,
        run=run,
        source_map=source_map,
        source_inventory=full_source_inventory,
        run_view=evidence_toolset.run_view if evidence_toolset is not None else None,
    )
    workspace, _ = await prepare_research_jobs_for_resume(
        db,
        run=run,
        workspace=workspace,
        save_workspace=save_research_workspace,
    )
    targeted_repair_active = pending_repair_operation(workspace, TARGETED_REPAIR_BUDGET) is not None
    if targeted_repair_active:
        start_repair_operation_in_workspace(workspace, TARGETED_REPAIR_BUDGET)
        await save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
        await db.commit()
    planner_source_map_summary = source_map_research_summary(source_map, full_source_inventory, include_evidence_ids=False)
    job_source_map_summary = source_map_research_summary(source_map, full_source_inventory, include_evidence_ids=True)
    try:
        if run_planner and workspace.get("status") == "planning":
            planner_tools = build_research_workspace_tools(
                db=db,
                run=run,
                workspace=workspace,
                source_inventory=full_source_inventory,
                source_map=source_map,
                evidence_toolset=evidence_toolset,
                mode="planner",
            )
            await build_research_planner_agent(
                db=db,
                run=run,
                runtime=runtime,
                workspace_summary=research_workspace_summary_for_prompt(
                    workspace,
                    include_atom_evidence_ids=False,
                    include_gap_evidence_ids=False,
                ),
                source_map_summary=planner_source_map_summary,
                workspace_tools=planner_tools,
                show_bible=show_bible,
            )
            workspace = await load_research_workspace(db, run.id) or workspace
        if not (workspace.get("investigation_jobs") or []):
            raise RuntimeError("Research workspace has no investigation jobs.")
        for job in list(unprocessed_research_jobs(workspace)):
            job_id = str(job.get("job_id") or "")
            if not job_id:
                continue
            workspace = await start_research_job(db, run=run, workspace=workspace, job_id=job_id)
            current_job = next((item for item in workspace.get("investigation_jobs") or [] if item.get("job_id") == job_id), job)
            scoped_toolset: EvidenceToolset | None = None
            if evidence_toolset is not None:
                allowed_refs = None if current_job.get("allow_broad_search") else {str(ref) for ref in current_job.get("source_refs") or []}
                scoped_toolset = EvidenceToolset(
                    db=db,
                    run=run,
                    run_view=evidence_toolset.run_view,
                    provider_index=evidence_toolset.provider_index,
                    research_allowed_source_refs=allowed_refs,
                    research_current_job_id=job_id,
                )
            job_workspace_tools = build_research_workspace_tools(
                db=db,
                run=run,
                workspace=workspace,
                source_inventory=full_source_inventory,
                source_map=source_map,
                evidence_toolset=scoped_toolset or evidence_toolset,
                mode="job",
                current_job_id=job_id,
            )
            job_tools = [
                *((scoped_toolset.agent_tools() if scoped_toolset is not None else evidence_tools) or []),
                *job_workspace_tools,
            ]
            await build_research_job_agent(
                db=db,
                run=run,
                runtime=runtime,
                job_payload=research_job_prompt_payload(
                    workspace,
                    job_id,
                    source_map_summary=job_source_map_summary,
                ),
                job_tools=job_tools,
                hosted_file_search_handler=hosted_file_search_handler,
            )
            workspace = await load_research_workspace(db, run.id) or workspace
            refreshed_job = next((item for item in workspace.get("investigation_jobs") or [] if item.get("job_id") == job_id), {})
            if refreshed_job.get("status") in {"planned", "in_progress"}:
                raise RuntimeError(f"Research job did not finish durably: {job_id}")
        unresolved_jobs = [
            str(item.get("job_id") or "")
            for item in workspace.get("investigation_jobs") or []
            if item.get("status") not in {"mapped", "deferred"}
        ]
        if unresolved_jobs:
            raise RuntimeError("Research finalization blocked by unresolved jobs: " + ", ".join(unresolved_jobs))
        workspace = dict(workspace)
        workspace["memo_fields"] = {}
        workspace["status"] = "finalizing"
        await save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
        finalizer_tools = build_research_workspace_tools(
            db=db,
            run=run,
            workspace=workspace,
            source_inventory=full_source_inventory,
            source_map=source_map,
            evidence_toolset=evidence_toolset,
            mode="finalizer",
        )
        await build_research_finalizer_agent(
            db=db,
            run=run,
            runtime=runtime,
            workspace_summary=research_finalizer_payload_for_prompt(workspace),
            finalizer_tools=finalizer_tools,
            show_bible=show_bible,
        )
        workspace = await load_research_workspace(db, run.id) or workspace
        if not workspace.get("memo_fields"):
            raise RuntimeError("Research finalizer did not finalize memo fields.")
        if targeted_repair_active:
            complete_repair_operation_in_workspace(workspace, TARGETED_REPAIR_BUDGET)
            await save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
            await db.commit()
        return research_memo_from_workspace(run, workspace)
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_editorial_research", "research_planner_agent", exc)
        return _fallback_research_memo(run, source_cards, quote_cards)


async def build_research_memo_ephemeral(
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_map: SourceMap,
    source_inventory: SourceMap,
    evidence_tools: list[AgentTool] | None,
    show_bible: ShowBibleContext | None,
) -> ResearchMemo:
    noop_workspace_tools = _noop_research_workspace_tools()
    planner_source_map_summary = source_map_research_summary(source_map, source_inventory, include_evidence_ids=False)
    job_source_map_summary = source_map_research_summary(source_map, source_inventory, include_evidence_ids=True)
    workspace = {
        "status": "planned",
        "source_order": [f"S{idx:02d}" for idx, _ in enumerate(source_inventory.source_cards, start=1)],
        "investigation_jobs": [
            {
                "job_id": "ephemeral_job",
                "label": "Ephemeral research capture test job",
                "story_job": "general",
                "source_refs": [f"S{idx:02d}" for idx, _ in enumerate(source_inventory.source_cards[:3], start=1)],
                "questions": ["Inspect the corpus for editorial atoms."],
                "status": "planned",
            }
        ],
        "atom_drafts": [],
    }
    try:
        planner = await build_research_planner_agent(
            db=None,  # type: ignore[arg-type]
            run=run,
            runtime=runtime,
            workspace_summary=research_workspace_summary_for_prompt(
                workspace,
                include_atom_evidence_ids=False,
                include_gap_evidence_ids=False,
            ),
            source_map_summary=planner_source_map_summary,
            workspace_tools=noop_workspace_tools,
            show_bible=show_bible,
        )
        await build_research_job_agent(
            db=None,  # type: ignore[arg-type]
            run=run,
            runtime=runtime,
            job_payload=research_job_prompt_payload(workspace, "ephemeral_job", source_map_summary=job_source_map_summary),
            job_tools=[*(evidence_tools or []), *noop_workspace_tools],
        )
        await build_research_finalizer_agent(
            db=None,  # type: ignore[arg-type]
            run=run,
            runtime=runtime,
            workspace_summary=research_finalizer_payload_for_prompt(workspace),
            finalizer_tools=noop_workspace_tools,
            show_bible=show_bible,
        )
        if isinstance(planner, ResearchMemo):
            return planner
        response = getattr(runtime, "response", None)
        if isinstance(response, ResearchMemo):
            return response
        return _fallback_research_memo(run, source_map.source_cards, source_map.quote_cards)
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_editorial_research", "research_planner_agent", exc)
        return _fallback_research_memo(run, source_map.source_cards, source_map.quote_cards)
