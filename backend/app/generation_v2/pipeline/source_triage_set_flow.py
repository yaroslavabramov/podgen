from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_triage_set_agent
from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from app.generation_v2.runtime import AgentOperationalStall, AgentTerminalNotReached
from app.generation_v2.runtime_checkpoint_state import commit_runtime_side_effects
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion
from app.generation_v2.source_corpus import CorpusPreparationResult
from app.generation_v2.source_triage_workspace import (
    build_workspace_tools,
    load_source_triage_workspace,
    source_set_prompt_payload,
    start_triage_source_set,
)
from app.generation_v2.tools.evidence import EvidenceToolset
from app.models.generation_run import GenerationRun
from app.pipeline.step_control import assert_step_not_stopped


class SourceTriageSetOperationalError(RuntimeError):
    """The set agent stopped twice without finishing or saving semantic progress."""


_TERMINAL_SET_STATUSES = {"mapped", "deferred"}
_SET_FINISH_ONLY_FIELDS = {
    "status",
    "started_at",
    "updated_at",
    "findings_summary",
    "deferred_reason",
    "written_actions",
    "pending_exact_reads",
    "pending_discovered_question_attempts",
}
_WORKSPACE_EXECUTION_COLLECTIONS = {
    "source_sets",
    "processed_sets",
    "mapped_sources",
    "deferred_sources",
}


async def run_source_triage_set(
    *,
    run: GenerationRun,
    db: AsyncSession,
    runtime: Any,
    corpus: CorpusPreparationResult,
    source_inventory: SourceMap,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> SourceTriageCompletion:
    set_id = str(source_set.get("set_id") or "")
    if not set_id:
        raise SourceTriageSetOperationalError("Source triage set is missing set_id; execution cannot continue.")
    bootstrap_toolset = EvidenceToolset(
        db=db,
        run=run,
        run_view=corpus.run_view,
        provider_index=corpus.provider_index,
    )
    workspace = await load_source_triage_workspace(db, run.id) or workspace
    persisted_set = next(
        (item for item in workspace.get("source_sets") or [] if item.get("set_id") == set_id),
        source_set,
    )
    if persisted_set.get("status") in _TERMINAL_SET_STATUSES:
        return _completion_from_finished_set(persisted_set)
    if persisted_set.get("status") != "in_progress":
        workspace = await start_triage_source_set(
            db,
            run=run,
            workspace=workspace,
            set_id=set_id,
            inventory=source_inventory,
            evidence_toolset=bootstrap_toolset,
        )
    no_progress_returns = 0
    activation = 0

    while True:
        await assert_step_not_stopped()
        activation += 1
        current = await load_source_triage_workspace(db, run.id) or workspace
        current_set = next(
            (item for item in current.get("source_sets") or [] if item.get("set_id") == set_id),
            source_set,
        )
        if current_set.get("status") in _TERMINAL_SET_STATUSES:
            return _completion_from_finished_set(current_set)
        if current_set.get("status") != "in_progress":
            current = await start_triage_source_set(
                db,
                run=run,
                workspace=current,
                set_id=set_id,
                inventory=source_inventory,
                evidence_toolset=bootstrap_toolset,
            )
            current_set = next(
                (item for item in current.get("source_sets") or [] if item.get("set_id") == set_id),
                current_set,
            )

        progress_before = _set_progress_state(current, set_id)
        allowed_refs = {str(ref) for ref in current_set.get("source_refs") or [] if str(ref).strip()}
        repair_write_refs = (
            {str(ref) for ref in current_set.get("repair_source_refs") or [] if str(ref).strip()}
            if current_set.get("repair_targeted")
            else None
        )
        scoped_toolset = EvidenceToolset(
            db=db,
            run=run,
            run_view=corpus.run_view,
            provider_index=corpus.provider_index,
            triage_allowed_source_refs=allowed_refs,
            triage_write_allowed_source_refs=repair_write_refs,
            triage_current_set_id=set_id,
        )
        set_tools = [
            *scoped_toolset.agent_tools(),
            *build_workspace_tools(
                db=db,
                run=run,
                workspace=current,
                inventory=source_inventory,
                evidence_toolset=scoped_toolset,
                mode="set",
                current_set_id=set_id,
            ),
        ]

        async def terminal_progress_loader() -> dict[str, Any]:
            latest_state = await load_source_triage_workspace(db, run.id) or current
            return _set_progress_state(latest_state, set_id)

        try:
            completion = await build_source_triage_set_agent(
                db,
                run,
                runtime,
                source_set_payload=source_set_prompt_payload(current, set_id),
                set_tools=set_tools,
                vector_store_id=corpus.vector_store_id,
                hosted_file_search_handler=scoped_toolset.persist_hosted_file_search_calls,
                output_language=str((current.get("intent") or {}).get("language") or "").strip() or None,
                terminal_progress_loader=terminal_progress_loader,
            )
        except AgentOperationalStall as exc:
            latest = await load_source_triage_workspace(db, run.id) or current
            reason = str(exc)
            await mark_source_triage_set_unfinished(
                db=db,
                run=run,
                workspace=latest,
                set_id=set_id,
                reason=reason,
                activation=activation,
                progress_saved=False,
                stalled=True,
            )
            raise SourceTriageSetOperationalError(
                f"Source triage set {set_id} stalled: {reason}"
            ) from exc
        except AgentTerminalNotReached as exc:
            if exc.repeated_without_progress:
                latest = await load_source_triage_workspace(db, run.id) or current
                reason = str(exc)
                await mark_source_triage_set_unfinished(
                    db=db,
                    run=run,
                    workspace=latest,
                    set_id=set_id,
                    reason=reason,
                    activation=activation,
                    progress_saved=False,
                    stalled=True,
                )
                raise SourceTriageSetOperationalError(
                    f"Source triage set {set_id} stalled: {reason}"
                ) from exc
            completion = SourceTriageCompletion(ready=False, summary=str(exc), notes=[])
        latest = await load_source_triage_workspace(db, run.id) or current
        finished_set = next(
            (item for item in latest.get("source_sets") or [] if item.get("set_id") == set_id),
            None,
        )
        if finished_set is not None and finished_set.get("status") in _TERMINAL_SET_STATUSES:
            return completion

        progress_saved = _set_progress_state(latest, set_id) != progress_before
        no_progress_returns = 0 if progress_saved else no_progress_returns + 1
        stalled = no_progress_returns >= 2
        reason = (
            "Set agent returned twice without calling finish_triage_source_set or saving semantic progress."
            if stalled
            else "Set agent returned before calling finish_triage_source_set; reactivating the same set."
        )
        await mark_source_triage_set_unfinished(
            db=db,
            run=run,
            workspace=latest,
            set_id=set_id,
            reason=reason,
            activation=activation,
            progress_saved=progress_saved,
            stalled=stalled,
        )
        if stalled:
            raise SourceTriageSetOperationalError(f"Source triage set {set_id} stalled: {reason}")
        workspace = latest


async def mark_source_triage_set_unfinished(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    set_id: str,
    reason: str,
    activation: int | None = None,
    progress_saved: bool = False,
    stalled: bool = False,
) -> None:
    del workspace
    from app.generation_v2.artifacts import save_tool_trace

    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_TRIAGE,
        agent_name=V2AgentName.SOURCE_TRIAGE_SET_WORKFLOW,
        tool_name=V2ToolName.SOURCE_SET_UNFINISHED,
        input_json={"set_id": set_id, "activation": activation},
        output_json={
            "accepted": False,
            "error": reason,
            "progress_saved": progress_saved,
            "reactivate_same_set": not stalled,
            "operational_failure": stalled,
        },
    )
    await commit_runtime_side_effects(db)


def _completion_from_finished_set(source_set: dict[str, Any]) -> SourceTriageCompletion:
    deferred_reason = str(source_set.get("deferred_reason") or "").strip()
    return SourceTriageCompletion(
        ready=True,
        summary=str(source_set.get("findings_summary") or "Source triage set already persisted."),
        notes=[deferred_reason] if deferred_reason else [],
    )


def _set_progress_state(workspace: dict[str, Any], set_id: str) -> dict[str, Any]:
    """Return durable semantic state while ignoring execution/finish bookkeeping."""

    source_set = next(
        (dict(item) for item in workspace.get("source_sets") or [] if item.get("set_id") == set_id),
        {},
    )
    set_state = {
        key: _canonical_progress_value(value)
        for key, value in source_set.items()
        if key not in _SET_FINISH_ONLY_FIELDS
    }
    records: dict[str, Any] = {}
    for collection_name, value in workspace.items():
        if collection_name in _WORKSPACE_EXECUTION_COLLECTIONS or not isinstance(value, list):
            continue
        matching = [
            _canonical_progress_value(item)
            for item in value
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ]
        if matching:
            records[collection_name] = sorted(
                matching,
                key=lambda item: (
                    _progress_record_identity(collection_name, item),
                    _progress_sort_key(item),
                ),
            )
    return {"source_set": set_state, "records": records}


def _canonical_progress_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical_progress_value(item)
            for key, item in value.items()
            if key
            not in {
                "created_at",
                "started_at",
                "updated_at",
                "semantic_revision",
                "input_revision",
            }
        }
    if isinstance(value, list):
        normalized = [_canonical_progress_value(item) for item in value]
        return sorted(normalized, key=_progress_sort_key)
    return value


def _progress_record_identity(collection_name: str, item: dict[str, Any]) -> tuple[str, ...]:
    identity_fields = {
        "source_assessments": ("source_ref", "source_id", "assessment_id"),
        "source_findings": ("finding_id", "source_ref"),
        "coverage_outcomes": ("question_id", "outcome_id"),
        "coverage_syntheses": ("question_id", "outcome_id"),
    }.get(collection_name, ("id", "set_id"))
    identity = tuple(str(item.get(field) or "") for field in identity_fields)
    return identity if any(identity) else (_progress_sort_key(item),)


def _progress_sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

__all__ = [
    "SourceTriageSetOperationalError",
    "assert_step_not_stopped",
]
