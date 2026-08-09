from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_mapper_agent
from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from app.generation_v2.runtime import AgentOperationalStall, AgentTerminalNotReached
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion
from app.generation_v2.source_corpus import CorpusPreparationResult
from app.generation_v2.source_triage_workspace import (
    build_workspace_tools,
    load_source_triage_workspace,
    save_source_triage_workspace,
    start_triage_source_set,
)
from app.generation_v2.tools.evidence import EvidenceToolset
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.models.generation_run import GenerationRun
from app.pipeline.step_control import assert_step_not_stopped


class SourceMapperOperationalError(RuntimeError):
    """A one-source mapper stopped without a terminal SourceAnalysis."""


async def run_source_mapper_for_set(
    *,
    run: GenerationRun,
    db: AsyncSession,
    runtime: Any,
    corpus: CorpusPreparationResult,
    source_inventory: SourceMap,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> list[SourceTriageCompletion]:
    set_id = str(source_set.get("set_id") or "")
    if not set_id:
        raise SourceMapperOperationalError("Source routing set is missing set_id.")
    bootstrap_toolset = EvidenceToolset(
        db=db,
        run=run,
        run_view=corpus.run_view,
        provider_index=corpus.provider_index,
    )
    workspace = await start_triage_source_set(
        db,
        run=run,
        workspace=workspace,
        set_id=set_id,
        inventory=source_inventory,
        evidence_toolset=bootstrap_toolset,
    )
    completions: list[SourceTriageCompletion] = []
    for source_ref in [str(item) for item in source_set.get("source_refs") or [] if str(item).strip()]:
        await assert_step_not_stopped()
        latest = await load_source_triage_workspace(db, run.id) or workspace
        if _source_is_finished(latest, source_ref):
            completions.append(
                SourceTriageCompletion(
                    ready=True,
                    summary=f"Source {source_ref} mapping already persisted.",
                    notes=[],
                )
            )
            continue
        completion = await run_source_mapper(
            run=run,
            db=db,
            runtime=runtime,
            corpus=corpus,
            source_inventory=source_inventory,
            workspace=latest,
            source_set=source_set,
            source_ref=source_ref,
        )
        completions.append(completion)
        workspace = await load_source_triage_workspace(db, run.id) or latest
    return completions


async def run_source_mapper(
    *,
    run: GenerationRun,
    db: AsyncSession,
    runtime: Any,
    corpus: CorpusPreparationResult,
    source_inventory: SourceMap,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    source_ref: str,
) -> SourceTriageCompletion:
    set_id = str(source_set.get("set_id") or "")
    no_progress_returns = 0
    activation = 0
    while True:
        await assert_step_not_stopped()
        activation += 1
        current = await load_source_triage_workspace(db, run.id) or workspace
        current["current_set"] = {
            "set_id": set_id,
            "source_refs": list(source_set.get("source_refs") or []),
        }
        current["current_source_mapping"] = {
            "set_id": set_id,
            "source_ref": source_ref,
            "activation": activation,
        }
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
        progress_before = _source_progress_state(current, source_ref)
        scoped_toolset = EvidenceToolset(
            db=db,
            run=run,
            run_view=corpus.run_view,
            provider_index=corpus.provider_index,
            triage_allowed_source_refs={source_ref},
            triage_write_allowed_source_refs={source_ref},
            triage_current_set_id=set_id,
        )
        mapper_tools = [
            *source_mapper_evidence_tools(scoped_toolset),
            *build_workspace_tools(
                db=db,
                run=run,
                workspace=current,
                inventory=source_inventory,
                evidence_toolset=scoped_toolset,
                mode="source_mapper",
                current_set_id=set_id,
            ),
        ]

        async def terminal_progress_loader() -> dict[str, Any]:
            latest = await load_source_triage_workspace(db, run.id) or current
            return {
                "source_ref": source_ref,
                "has_current_analysis": _source_has_analysis(latest, source_ref),
                "finished": _source_is_finished(latest, source_ref),
            }

        try:
            completion = await build_source_mapper_agent(
                db,
                run,
                runtime,
                source_payload=_source_mapper_prompt_payload(
                    current,
                    source_inventory=source_inventory,
                    source_set=source_set,
                    source_ref=source_ref,
                ),
                mapper_tools=mapper_tools,
                vector_store_id=corpus.vector_store_id,
                hosted_file_search_handler=scoped_toolset.persist_hosted_file_search_calls,
                output_language=str((current.get("intent") or {}).get("language") or "").strip() or None,
                terminal_progress_loader=terminal_progress_loader,
            )
        except AgentOperationalStall as exc:
            await _record_source_mapper_unfinished(
                db=db,
                run=run,
                source_ref=source_ref,
                set_id=set_id,
                activation=activation,
                reason=str(exc),
                stalled=True,
            )
            raise SourceMapperOperationalError(f"Source mapper {source_ref} stalled: {exc}") from exc
        except AgentTerminalNotReached as exc:
            if exc.repeated_without_progress:
                await _record_source_mapper_unfinished(
                    db=db,
                    run=run,
                    source_ref=source_ref,
                    set_id=set_id,
                    activation=activation,
                    reason=str(exc),
                    stalled=True,
                )
                raise SourceMapperOperationalError(f"Source mapper {source_ref} stalled: {exc}") from exc
            completion = SourceTriageCompletion(ready=False, summary=str(exc), notes=[])

        latest = await load_source_triage_workspace(db, run.id) or current
        if _source_is_finished(latest, source_ref):
            return completion
        progress_saved = _source_progress_state(latest, source_ref) != progress_before
        no_progress_returns = 0 if progress_saved else no_progress_returns + 1
        stalled = no_progress_returns >= 2
        reason = (
            f"Source mapper returned twice without finish_source_mapping or saved progress for {source_ref}."
            if stalled
            else f"Source mapper returned before finish_source_mapping for {source_ref}; reactivating same source."
        )
        await _record_source_mapper_unfinished(
            db=db,
            run=run,
            source_ref=source_ref,
            set_id=set_id,
            activation=activation,
            reason=reason,
            stalled=stalled,
        )
        if stalled:
            raise SourceMapperOperationalError(reason)
        workspace = latest


def _source_mapper_prompt_payload(
    workspace: dict[str, Any],
    *,
    source_inventory: SourceMap,
    source_set: dict[str, Any],
    source_ref: str,
) -> dict[str, Any]:
    ref_context = build_source_ref_context(source_inventory)
    card = next(
        (
            item
            for item in source_inventory.source_cards
            if source_ref_for_id(item.source_id, ref_context) == source_ref
        ),
        None,
    )
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    parent_ids = {
        str(item.get("parent_question_id") or "")
        for item in questions
        if str(item.get("parent_question_id") or "").strip()
    }
    return {
        "source_ref": source_ref,
        "set_id": source_set.get("set_id"),
        "source": (
            {
                "title": card.title,
                "source_type": card.source_type,
                "url": card.url,
                "domain": card.domain,
                "published_at": card.published_at,
                "summary": card.summary,
                "gaps_or_cautions": list(card.gaps_or_cautions),
                "duplicate_of_source_ref": "",
            }
            if card is not None
            else {"source_ref": source_ref}
        ),
        "question_index": [
            {
                "question_id": item.get("question_id"),
                "question": item.get("question"),
                "kind": item.get("kind"),
                "importance": item.get("importance"),
                "parent_question_id": item.get("parent_question_id"),
                "is_leaf": str(item.get("question_id") or "") not in parent_ids,
                "requirements": [
                    {
                        "requirement_id": requirement.get("requirement_id"),
                        "description": requirement.get("description"),
                        "completion_criterion": requirement.get("completion_criterion"),
                    }
                    for requirement in item.get("answer_requirements") or []
                    if isinstance(requirement, dict)
                ],
            }
            for item in questions
        ],
        "leaf_question_ids": [
            str(item.get("question_id") or "")
            for item in questions
            if str(item.get("question_id") or "") and str(item.get("question_id") or "") not in parent_ids
        ],
    }


def _source_has_analysis(workspace: dict[str, Any], source_ref: str) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("source_ref") or "") == source_ref
        and item.get("current") is not False
        and item.get("accepted") is not False
        for item in workspace.get("source_analyses") or []
    )


def _source_is_finished(workspace: dict[str, Any], source_ref: str) -> bool:
    return any(
        isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref
        for item in workspace.get("mapped_sources") or []
    )


def _source_progress_state(workspace: dict[str, Any], source_ref: str) -> tuple[Any, ...]:
    return (
        [
            item
            for item in workspace.get("source_analyses") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref
        ],
        [
            item
            for item in workspace.get("mapped_sources") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref
        ],
    )


def source_mapper_evidence_tools(evidence_toolset: Any) -> list[Any]:
    return [tool for tool in evidence_toolset.agent_tools() if getattr(tool, "name", "") == "read_source"]


async def _record_source_mapper_unfinished(
    *,
    db: AsyncSession,
    run: GenerationRun,
    source_ref: str,
    set_id: str,
    activation: int,
    reason: str,
    stalled: bool,
) -> None:
    from app.generation_v2.artifacts import save_tool_trace

    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_TRIAGE,
        agent_name=V2AgentName.SOURCE_MAPPER_WORKFLOW,
        tool_name=V2ToolName.SOURCE_SET_UNFINISHED,
        input_json={"set_id": set_id, "source_ref": source_ref, "activation": activation},
        output_json={"accepted": False, "reason": reason, "stalled": stalled},
    )


__all__ = [
    "SourceMapperOperationalError",
    "run_source_mapper",
    "run_source_mapper_for_set",
    "source_mapper_evidence_tools",
]
