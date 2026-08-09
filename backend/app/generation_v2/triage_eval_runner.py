from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import uuid
from typing import Any

from app.database import async_session_factory
from app.generation_v2.artifact_costs import summarize_tool_trace_costs
from app.generation_v2.pipeline.source_triage_stage import SourceTriageStageRunner
from app.generation_v2.pipeline.source_triage_source_mapper_flow import run_source_mapper_for_set
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.source_corpus import (
    load_current_v2_corpus,
    preview_v2_corpus_manifest_hash,
)
from app.generation_v2.source_corpus_run_view import load_current_run_corpus_view
from app.generation_v2.source_triage_workspace import (
    load_source_triage_workspace,
    save_source_triage_workspace,
    unprocessed_source_sets,
)
from app.generation_v2.source_triage_plan_validation import triage_plan_violations
from app.generation_v2.triage_eval_support import (
    EvalBudgetExceeded,
    EvalCostGuard,
    emit as _emit,
    execute_monitored_eval,
)
from app.generation_v2.triage_semantic_eval_report import semantic_eval_report
from app.generation_v2.schemas import SourceTriageCompletion
from app.models.generation_run import GenerationRun, GenerationStatus


TRIAGE_FIRST_SET_EVAL_MAX_COST_USD = 1.70
TRIAGE_FULL_TRIAGE_EVAL_MAX_COST_USD = 8.00
TRIAGE_EVAL_STOP_FIRST_SET = "first_set"
TRIAGE_EVAL_STOP_FULL_TRIAGE = "full_triage"
TRIAGE_EVAL_GOLDEN_HONG_KONG = "hong_kong"
HONG_KONG_GOLDEN_MANIFEST_HASH = "4edb09781e1950b4b2cc3e690975b6af34d24fdd773555565ed5449824559c03"
HONG_KONG_GOLDEN_FIRST_SET_SOURCE_REFS = ["S03"]
HONG_KONG_GOLDEN_FULL_SOURCE_REFS = ["S01", "S03"]
HONG_KONG_GOLDEN_FIRST_SET_SALIENT_FACTS = [
    {
        "label": "s03_private_notes_coins_demand_hkma_chain",
        "source_ref": "S03",
        "question_id": "q_hkd_current_mechanics",
        "required_term_groups": [
            ["частн", "private"],
            ["банкнот", "notes"],
            ["монет", "coin"],
            ["спрос", "demand"],
            ["HKMA"],
            ["HK$10", "$10"],
        ],
    },
    {
        "label": "s03_ci_exchange_fund_lers_mechanism",
        "source_ref": "S03",
        "question_id": "q_hkd_current_mechanics",
        "required_term_groups": [
            ["certificate", "сертификат", "CI"],
            ["exchange fund"],
            ["LERS", "linked exchange rate", "связанн"],
        ],
    },
]
HONG_KONG_GOLDEN_FULL_ONLY_SALIENT_FACTS = [
    {
        "label": "s01_government_hk10",
        "source_ref": "S01",
        "question_id": "q_hkd_current_mechanics",
        "required_term_groups": [
            ["HK$10", "$10"],
            ["правитель", "государств", "government"],
            ["выпуска", "issue", "issued"],
        ],
    },
]
HONG_KONG_GOLDEN_GAPS = [
    {
        "label": "s03_hk10_specific_exception_gap",
        "source_ref": "S03",
        "question_id": "q_hkd_current_mechanics",
        "required_term_groups": [
            ["HK$10", "$10"],
            ["почему", "причин", "объясн", "мотив"],
        ],
    },
    {
        "label": "early_private_note_legal_basis_gap",
        "source_ref": "S03",
        "question_id": "q_hkd_origin_private_notes",
        "required_term_groups": [
            ["правов", "юрид", "legal"],
            ["основан", "баз", "basis"],
        ],
    },
]


async def preflight_first_set_eval(
    source_run_id: uuid.UUID,
    *,
    mode: str = TRIAGE_EVAL_STOP_FIRST_SET,
    expected_manifest_hash: str = "",
    expected_source_refs: list[str] | None = None,
    expected_salient_facts: list[dict[str, Any]] | None = None,
    expected_gaps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a frozen-plan canary without cloning a run or calling a model."""

    async with async_session_factory() as db:
        source = await db.get(GenerationRun, source_run_id)
        workspace = await load_source_triage_workspace(db, source_run_id)
        report = _eval_readiness_report(source=source, workspace=workspace)
        if source is None:
            return report
        source_view = await load_current_run_corpus_view(db, source_run_id)
        if source_view is None:
            return _append_readiness_issue(report, "source run has no pinned corpus view")
        try:
            current_manifest_hash = await preview_v2_corpus_manifest_hash(db, source)
        except RuntimeError as exc:
            return _append_readiness_issue(report, f"current corpus is not eval-ready: {exc}")
        if current_manifest_hash != source_view.manifest_hash:
            return _append_readiness_issue(
                report,
                "current project corpus differs from the source run",
            )
        report = _append_golden_preflight_issues(
            report,
            workspace=workspace or {},
            source_manifest_hash=str(source_view.manifest_hash or ""),
            mode=mode,
            expected_manifest_hash=expected_manifest_hash,
            expected_source_refs=expected_source_refs or [],
            expected_salient_facts=expected_salient_facts or [],
            expected_gaps=expected_gaps or [],
        )
        return report


async def run_first_set_eval(
    *,
    source_run_id: uuid.UUID,
    max_cost_usd: float,
    expected_salient_facts: list[dict[str, Any]] | None = None,
    expected_gaps: list[dict[str, Any]] | None = None,
    expected_manifest_hash: str = "",
    expected_source_refs: list[str] | None = None,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    if not (expected_salient_facts or expected_gaps):
        raise ValueError("triage eval requires --expect-salient-fact or --expect-gap")
    if max_cost_usd <= 0 or max_cost_usd > TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:
        raise ValueError(
            f"max_cost_usd must be within (0, {TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:.2f}]"
        )
    readiness = await preflight_first_set_eval(
        source_run_id,
        mode=TRIAGE_EVAL_STOP_FIRST_SET,
        expected_manifest_hash=expected_manifest_hash,
        expected_source_refs=expected_source_refs or [],
        expected_salient_facts=expected_salient_facts or [],
        expected_gaps=expected_gaps or [],
    )
    _emit("eval_preflight", **readiness)
    if readiness["ready"] is not True:
        raise RuntimeError("Triage eval preflight failed: " + "; ".join(readiness["issues"]))
    eval_run = await _clone_eval_run(
        source_run_id,
        max_cost_usd=max_cost_usd,
        stop_after=TRIAGE_EVAL_STOP_FIRST_SET,
        expected_salient_facts=expected_salient_facts or [],
        expected_gaps=expected_gaps or [],
        expected_manifest_hash=expected_manifest_hash,
        expected_source_refs=expected_source_refs or [],
    )
    return await execute_monitored_eval(
        run_id=eval_run.id,
        worker_coro=_execute_first_set(
            eval_run_id=eval_run.id,
            source_run_id=source_run_id,
            max_cost_usd=max_cost_usd,
        ),
        max_cost_usd=max_cost_usd,
        poll_seconds=poll_seconds,
    )


async def resume_first_set_eval(
    *,
    eval_run_id: uuid.UUID,
    max_cost_usd: float,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    """Continue one provider-failed canary from its persisted set memory."""

    if max_cost_usd <= 0 or max_cost_usd > TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:
        raise ValueError(
            f"max_cost_usd must be within (0, {TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:.2f}]"
        )
    run, source_run_id = await _prepare_eval_resume(
        eval_run_id,
        expected_stop_after=TRIAGE_EVAL_STOP_FIRST_SET,
    )
    _emit(
        "eval_resumed",
        run_id=str(run.id),
        source_run_id=str(source_run_id),
        max_cost_usd=max_cost_usd,
    )
    return await execute_monitored_eval(
        run_id=run.id,
        worker_coro=_execute_first_set(
            eval_run_id=run.id,
            source_run_id=source_run_id,
            max_cost_usd=max_cost_usd,
            resume_existing=True,
        ),
        max_cost_usd=max_cost_usd,
        poll_seconds=poll_seconds,
    )


async def run_full_triage_eval(
    *,
    source_run_id: uuid.UUID,
    max_cost_usd: float,
    expected_salient_facts: list[dict[str, Any]] | None = None,
    expected_gaps: list[dict[str, Any]] | None = None,
    expected_manifest_hash: str = "",
    expected_source_refs: list[str] | None = None,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    if not (expected_salient_facts or expected_gaps):
        raise ValueError("triage eval requires --expect-salient-fact or --expect-gap")
    if max_cost_usd <= 0 or max_cost_usd > TRIAGE_FULL_TRIAGE_EVAL_MAX_COST_USD:
        raise ValueError(
            f"max_cost_usd must be within (0, {TRIAGE_FULL_TRIAGE_EVAL_MAX_COST_USD:.2f}]"
        )
    readiness = await preflight_first_set_eval(
        source_run_id,
        mode=TRIAGE_EVAL_STOP_FULL_TRIAGE,
        expected_manifest_hash=expected_manifest_hash,
        expected_source_refs=expected_source_refs or [],
        expected_salient_facts=expected_salient_facts or [],
        expected_gaps=expected_gaps or [],
    )
    _emit("eval_preflight", **readiness)
    if readiness["ready"] is not True:
        raise RuntimeError("Triage eval preflight failed: " + "; ".join(readiness["issues"]))
    eval_run = await _clone_eval_run(
        source_run_id,
        max_cost_usd=max_cost_usd,
        stop_after=TRIAGE_EVAL_STOP_FULL_TRIAGE,
        expected_salient_facts=expected_salient_facts or [],
        expected_gaps=expected_gaps or [],
        expected_manifest_hash=expected_manifest_hash,
        expected_source_refs=expected_source_refs or [],
    )
    return await execute_monitored_eval(
        run_id=eval_run.id,
        worker_coro=_execute_full_triage(
            eval_run_id=eval_run.id,
            source_run_id=source_run_id,
            max_cost_usd=max_cost_usd,
        ),
        max_cost_usd=max_cost_usd,
        poll_seconds=poll_seconds,
    )


async def resume_full_triage_eval(
    *,
    eval_run_id: uuid.UUID,
    max_cost_usd: float,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    if max_cost_usd <= 0 or max_cost_usd > TRIAGE_FULL_TRIAGE_EVAL_MAX_COST_USD:
        raise ValueError(
            f"max_cost_usd must be within (0, {TRIAGE_FULL_TRIAGE_EVAL_MAX_COST_USD:.2f}]"
        )
    run, source_run_id = await _prepare_eval_resume(
        eval_run_id,
        expected_stop_after=TRIAGE_EVAL_STOP_FULL_TRIAGE,
    )
    _emit(
        "eval_resumed",
        run_id=str(run.id),
        source_run_id=str(source_run_id),
        max_cost_usd=max_cost_usd,
    )
    return await execute_monitored_eval(
        run_id=run.id,
        worker_coro=_execute_full_triage(
            eval_run_id=run.id,
            source_run_id=source_run_id,
            max_cost_usd=max_cost_usd,
            resume_existing=True,
        ),
        max_cost_usd=max_cost_usd,
        poll_seconds=poll_seconds,
    )


async def _clone_eval_run(
    source_run_id: uuid.UUID,
    *,
    max_cost_usd: float,
    stop_after: str,
    expected_salient_facts: list[dict[str, Any]] | None = None,
    expected_gaps: list[dict[str, Any]] | None = None,
    expected_manifest_hash: str = "",
    expected_source_refs: list[str] | None = None,
) -> GenerationRun:
    async with async_session_factory() as db:
        source = await db.get(GenerationRun, source_run_id)
        if source is None:
            raise RuntimeError(f"Source generation run {source_run_id} was not found")
        config = dict(source.config or {})
        config["triage_eval"] = {
            "source_run_id": str(source_run_id),
            "plan_mode": "clean_copy_from_source_run",
            "stop_after": stop_after,
            "max_cost_usd": max_cost_usd,
            "expected_salient_facts": expected_salient_facts or [],
            "expected_gaps": expected_gaps or [],
            "expected_manifest_hash": expected_manifest_hash,
            "expected_source_refs": expected_source_refs or [],
        }
        run = GenerationRun(
            project_id=source.project_id,
            user_id=source.user_id,
            show_bible_id=source.show_bible_id,
            status=GenerationStatus.RUNNING,
            config=config,
            estimated_cost=source.estimated_cost,
            started_at=datetime.utcnow(),
            current_phase="v2_source_triage",
            current_step="triage eval: preparing corpus",
            progress_pct=0,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        _emit("eval_started", run_id=str(run.id), source_run_id=str(source_run_id), max_cost_usd=max_cost_usd)
        return run


async def _prepare_eval_resume(
    eval_run_id: uuid.UUID,
    *,
    expected_stop_after: str,
) -> tuple[GenerationRun, uuid.UUID]:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, eval_run_id)
        if run is None:
            raise RuntimeError(f"Evaluation run {eval_run_id} was not found")
        eval_config = dict((run.config or {}).get("triage_eval") or {})
        if eval_config.get("stop_after") != expected_stop_after:
            raise RuntimeError(f"Run is not a {expected_stop_after} triage evaluation")
        try:
            source_run_id = uuid.UUID(str(eval_config.get("source_run_id") or ""))
        except ValueError as exc:
            raise RuntimeError("Evaluation run has no valid source_run_id") from exc
        workspace = await load_source_triage_workspace(db, run.id)
        if workspace is None or not workspace.get("source_sets"):
            raise RuntimeError("Evaluation run has no persisted set state to resume")
        if expected_stop_after == TRIAGE_EVAL_STOP_FIRST_SET:
            first_set = next(
                (
                    item
                    for item in workspace.get("source_sets") or []
                    if isinstance(item, dict)
                ),
                {},
            )
            if first_set.get("status") in {"mapped", "deferred"}:
                raise RuntimeError("Evaluation first set is already complete")
        elif str(workspace.get("status") or "") == "finalized":
            raise RuntimeError("Evaluation source triage is already complete")
        run.status = GenerationStatus.RUNNING
        run.error_message = None
        run.completed_at = None
        run.current_step = "triage eval: resuming first set"
        await db.commit()
        await db.refresh(run)
        return run, source_run_id


async def _execute_first_set(
    *,
    eval_run_id: uuid.UUID,
    source_run_id: uuid.UUID,
    max_cost_usd: float,
    resume_existing: bool = False,
) -> dict[str, Any]:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, eval_run_id)
        if run is None:
            raise RuntimeError("Evaluation run disappeared before execution")
        persisted_workspace = None
        prepared_corpus = None
        if resume_existing:
            persisted_workspace = await load_source_triage_workspace(db, run.id)
            if persisted_workspace is None or not persisted_workspace.get("source_sets"):
                raise RuntimeError("Evaluation run lost its persisted set state")
            current_eval_view = await load_current_run_corpus_view(db, run.id)
            if current_eval_view is None or not _workspace_matches_run_view(
                persisted_workspace,
                current_eval_view,
            ):
                raise RuntimeError(
                    "Evaluation run's current corpus scope no longer matches its persisted set state"
                )
            prepared_corpus = await load_current_v2_corpus(db, run)
        runner = SourceTriageStageRunner(
            run,
            db,
            V2Runtime(before_provider_call=EvalCostGuard(max_cost_usd=max_cost_usd)),
            prepared_corpus=prepared_corpus,
        )
        context = await runner.prepare_context()
        source_view = await load_current_run_corpus_view(db, source_run_id)
        if source_view is None:
            raise RuntimeError("Source run has no pinned corpus view")
        if context.corpus.run_view.manifest_hash != source_view.manifest_hash:
            raise RuntimeError("Current project corpus differs from the source run; refusing a non-comparable eval")
        if resume_existing:
            workspace = await load_source_triage_workspace(db, run.id) or persisted_workspace
            _emit(
                "set_state_restored",
                run_id=str(run.id),
                source_set_count=len(workspace.get("source_sets") or []),
            )
        else:
            source_workspace = await load_source_triage_workspace(db, source_run_id)
            if source_workspace is None:
                raise RuntimeError("Source run has no triage plan to copy")
            workspace = _clean_plan_workspace(
                fresh_workspace=context.workspace,
                source_workspace=source_workspace,
            )
            await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
            _emit(
                "plan_copied",
                run_id=str(run.id),
                source_run_id=str(source_run_id),
                source_set_count=len(workspace.get("source_sets") or []),
            )
        eval_config = dict((getattr(run, "config", None) or {}).get("triage_eval") or {})
        _raise_for_golden_workspace_mismatch(workspace, eval_config)
        pending_sets = unprocessed_source_sets(workspace)
        if not pending_sets:
            raise RuntimeError("Planner produced no source set for evaluation")
        source_set = pending_sets[0]
        set_id = str(source_set.get("set_id") or "")
        _emit(
            "first_set_started",
            run_id=str(run.id),
            set_id=set_id,
            source_count=len(source_set.get("source_refs") or []),
            question_count=len(source_set.get("question_ids") or []),
        )
        completions = await run_source_mapper_for_set(
            run=run,
            db=db,
            runtime=runner.runtime,
            corpus=context.corpus,
            source_inventory=context.source_inventory,
            workspace=workspace,
            source_set=source_set,
        )
        workspace = await load_source_triage_workspace(db, run.id) or workspace
        persisted_set = next(
            (
                item
                for item in workspace.get("source_sets") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
            ),
            {},
        )
        if persisted_set.get("status") not in {"mapped", "deferred"}:
            raise RuntimeError("First set returned without an accepted finish")
        cost = await summarize_tool_trace_costs(db, run.id)
        observed_cost = float(cost["totals"].get("estimated_token_cost_usd") or 0.0)
        if observed_cost > max_cost_usd:
            raise EvalBudgetExceeded(
                f"Triage eval exceeded cost ceiling ${max_cost_usd:.2f} at ${observed_cost:.8f}"
            )
        semantic_report = _source_mapping_semantic_expectation_report(
            workspace,
            expected_salient_facts=eval_config.get("expected_salient_facts") or [],
            expected_gaps=eval_config.get("expected_gaps") or [],
            research_handoff=[],
        )
        if semantic_report["passed"] is not True:
            raise RuntimeError(
                "First set failed semantic postflight: "
                + "; ".join(semantic_report["issues"])
            )
        run.status = GenerationStatus.COMPLETED
        run.current_step = "triage eval stopped after first set"
        run.progress_pct = 100
        run.actual_cost = cost
        run.completed_at = datetime.utcnow()
        await db.commit()
        result = {
            "run_id": str(run.id),
            "set_id": set_id,
            "set_status": persisted_set.get("status"),
            "source_refs": list(persisted_set.get("source_refs") or []),
            "question_ids": list(persisted_set.get("question_ids") or []),
            "completions": [item.model_dump(mode="json") for item in completions],
            "cost": cost["totals"],
            "structural_report": _first_set_structural_report(
                workspace,
                persisted_set,
            ),
            "semantic_report": semantic_report,
        }
        if result["structural_report"]["passed"] is not True:
            raise RuntimeError(
                "First set failed structural postflight: "
                + "; ".join(result["structural_report"]["issues"])
            )
        _emit("first_set_completed", **result)
        return result


async def _execute_full_triage(
    *,
    eval_run_id: uuid.UUID,
    source_run_id: uuid.UUID,
    max_cost_usd: float,
    resume_existing: bool = False,
) -> dict[str, Any]:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, eval_run_id)
        if run is None:
            raise RuntimeError("Evaluation run disappeared before execution")
        persisted_workspace = None
        prepared_corpus = None
        if resume_existing:
            persisted_workspace = await load_source_triage_workspace(db, run.id)
            if persisted_workspace is None or not persisted_workspace.get("source_sets"):
                raise RuntimeError("Evaluation run lost its persisted triage state")
            current_eval_view = await load_current_run_corpus_view(db, run.id)
            if current_eval_view is None or not _workspace_matches_run_view(
                persisted_workspace,
                current_eval_view,
            ):
                raise RuntimeError(
                    "Evaluation run's current corpus scope no longer matches its persisted triage state"
                )
            prepared_corpus = await load_current_v2_corpus(db, run)
        runner = SourceTriageStageRunner(
            run,
            db,
            V2Runtime(before_provider_call=EvalCostGuard(max_cost_usd=max_cost_usd)),
            prepared_corpus=prepared_corpus,
        )
        context = await runner.prepare_context()
        source_view = await load_current_run_corpus_view(db, source_run_id)
        if source_view is None:
            raise RuntimeError("Source run has no pinned corpus view")
        if context.corpus.run_view.manifest_hash != source_view.manifest_hash:
            raise RuntimeError("Current project corpus differs from the source run; refusing a non-comparable eval")
        if resume_existing:
            workspace = await load_source_triage_workspace(db, run.id) or persisted_workspace
            _emit(
                "set_state_restored",
                run_id=str(run.id),
                source_set_count=len(workspace.get("source_sets") or []),
            )
        else:
            source_workspace = await load_source_triage_workspace(db, source_run_id)
            if source_workspace is None:
                raise RuntimeError("Source run has no triage plan to copy")
            workspace = _clean_plan_workspace(
                fresh_workspace=context.workspace,
                source_workspace=source_workspace,
            )
            await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
            _emit(
                "plan_copied",
                run_id=str(run.id),
                source_run_id=str(source_run_id),
                source_set_count=len(workspace.get("source_sets") or []),
            )
        eval_config = dict((getattr(run, "config", None) or {}).get("triage_eval") or {})
        _raise_for_golden_workspace_mismatch(workspace, eval_config)

        set_completions: list[SourceTriageCompletion] = []
        while True:
            pending_sets = unprocessed_source_sets(workspace)
            if not pending_sets:
                break
            source_set = pending_sets[0]
            set_id = str(source_set.get("set_id") or "")
            _emit(
                "source_set_started",
                run_id=str(run.id),
                set_id=set_id,
                remaining_set_count=len(pending_sets),
                source_count=len(source_set.get("source_refs") or []),
                question_count=len(source_set.get("question_ids") or []),
            )
            completions = await run_source_mapper_for_set(
                run=run,
                db=db,
                runtime=runner.runtime,
                corpus=context.corpus,
                source_inventory=context.source_inventory,
                workspace=workspace,
                source_set=source_set,
            )
            set_completions.extend(completions)
            workspace = await load_source_triage_workspace(db, run.id) or workspace
            persisted_set = next(
                (
                    item
                    for item in workspace.get("source_sets") or []
                    if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
                ),
                {},
            )
            if persisted_set.get("status") not in {"mapped", "deferred"}:
                raise RuntimeError(f"Source set {set_id or '<missing>'} returned without an accepted finish")
            set_report = _source_set_structural_report(workspace, persisted_set)
            _emit(
                "source_set_completed",
                run_id=str(run.id),
                set_id=set_id,
                set_status=persisted_set.get("status"),
                structural_report=set_report,
            )
            if set_report["passed"] is not True:
                raise RuntimeError(
                    f"Source set {set_id or '<missing>'} failed structural postflight: "
                    + "; ".join(set_report["issues"])
                )

        _emit("source_mapping_materialization_started", run_id=str(run.id))
        source_map, finalizer_completion, workspace = await runner.materialize_source_mapping(context, workspace)
        triage_state = await runner.validate_repair_and_persist(
            context,
            workspace=workspace,
            source_map=source_map,
            planner_completion=SourceTriageCompletion(
                ready=True,
                summary="Planner output was copied from the source run for clean full-triage eval.",
                notes=[],
            ),
            set_completions=set_completions,
            finalizer_completion=finalizer_completion,
        )
        workspace = await load_source_triage_workspace(db, run.id) or workspace
        cost = await summarize_tool_trace_costs(db, run.id)
        observed_cost = float(cost["totals"].get("estimated_token_cost_usd") or 0.0)
        if observed_cost > max_cost_usd:
            raise EvalBudgetExceeded(
                f"Triage eval exceeded cost ceiling ${max_cost_usd:.2f} at ${observed_cost:.8f}"
            )
        report = _full_triage_structural_report(
            workspace=workspace,
            triage_state=triage_state,
        )
        if report["passed"] is not True:
            raise RuntimeError("Full triage failed structural postflight: " + "; ".join(report["issues"]))
        corpus_brief = getattr(triage_state, "corpus_brief", None)
        semantic_report = _source_mapping_semantic_expectation_report(
            workspace,
            expected_salient_facts=eval_config.get("expected_salient_facts") or [],
            expected_gaps=eval_config.get("expected_gaps") or [],
            research_handoff=list(getattr(corpus_brief, "research_handoff", []) or []) if corpus_brief is not None else [],
        )
        if semantic_report["passed"] is not True:
            raise RuntimeError(
                "Full triage failed semantic postflight: "
                + "; ".join(semantic_report["issues"])
            )
        run.status = GenerationStatus.COMPLETED
        run.current_step = "triage eval stopped after full source triage"
        run.progress_pct = 100
        run.actual_cost = cost
        run.completed_at = datetime.utcnow()
        await db.commit()
        result = {
            "run_id": str(run.id),
            "source_run_id": str(source_run_id),
            "source_set_count": len(workspace.get("source_sets") or []),
            "source_count": len(workspace.get("source_order") or []),
            "completion": triage_state.triage_completion.model_dump(mode="json"),
            "cost": cost["totals"],
            "structural_report": report,
            "semantic_report": semantic_report,
        }
        _emit("full_triage_completed", **result)
        return result


def _workspace_matches_run_view(
    workspace: dict[str, Any],
    run_view: Any,
) -> bool:
    return (
        int(workspace.get("corpus_revision") or 1) == int(getattr(run_view, "revision", 1) or 1)
        and int(workspace.get("attempt") or 1) == int(getattr(run_view, "attempt", 1) or 1)
        and str(workspace.get("manifest_hash") or "")
        == str(getattr(run_view, "manifest_hash", "") or "")
    )


def _clean_plan_workspace(
    *,
    fresh_workspace: dict[str, Any],
    source_workspace: dict[str, Any],
) -> dict[str, Any]:
    """Copy only planner-owned records; never carry an old set's execution memory."""

    source_order = [str(value) for value in fresh_workspace.get("source_order") or []]
    source_sets = [
        _clean_planned_set(item)
        for item in source_workspace.get("source_sets") or []
        if isinstance(item, dict)
    ]
    assigned_refs = [
        str(source_ref)
        for source_set in source_sets
        for source_ref in source_set.get("source_refs") or []
    ]
    if not source_sets or sorted(assigned_refs) != sorted(source_order) or len(assigned_refs) != len(set(assigned_refs)):
        raise RuntimeError("Source run plan does not assign the current pinned corpus exactly once")
    questions = [
        deepcopy(item)
        for item in source_workspace.get("coverage_questions") or []
        if isinstance(item, dict)
    ]
    question_ids = {str(item.get("question_id") or "") for item in questions}
    central_question_id = str(source_workspace.get("central_question_id") or "")
    if not questions or central_question_id not in question_ids:
        raise RuntimeError("Source run plan has no valid central coverage question")

    clean = deepcopy(fresh_workspace)
    clean.update(
        {
            "status": "planned",
            "coverage_questions": questions,
            "source_sets": source_sets,
            "central_question_id": central_question_id,
            "plan_summary": str(source_workspace.get("plan_summary") or ""),
            "current_set": None,
            "processed_sets": [],
            "source_assessments": [],
            "source_findings": [],
            "source_read_receipts": [],
            "reading_notes": [],
            "coverage_hints": [],
            "coverage_outcomes": [],
            "coverage_syntheses": [],
            "source_structure_pages": [],
            "source_triage_semantic_review_deliveries": [],
            "source_triage_question_workbench_deliveries": [],
            "source_triage_finding_catalog_deliveries": [],
            "source_triage_set_semantic_reviews": {},
            "source_triage_set_semantic_self_checks": {},
            "source_triage_finalizer_reads": {},
            "triage_semantic_revision": 0,
            "mapped_sources": [],
            "deferred_sources": [],
            "open_questions": [],
        }
    )
    return clean


def _clean_planned_set(source_set: dict[str, Any]) -> dict[str, Any]:
    planning_fields = (
        "set_id",
        "label",
        "purpose",
        "rationale",
        "priority",
        "set_kind",
        "source_refs",
        "cross_question_ids",
        "question_assignments",
        "repair",
    )
    clean = {field: deepcopy(source_set[field]) for field in planning_fields if field in source_set}
    assignments = [
        deepcopy(item)
        for item in source_set.get("question_assignments") or []
        if isinstance(item, dict) and str(item.get("question_id") or "")
    ]
    planned_question_ids = [
        str(value)
        for value in source_set.get("planned_question_ids") or []
        if str(value)
    ]
    if not planned_question_ids:
        planned_question_ids = list(
            dict.fromkeys(str(item.get("question_id") or "") for item in assignments)
        )
    if not planned_question_ids:
        planned_question_ids = [
            str(value) for value in source_set.get("question_ids") or [] if str(value)
        ]
    clean["question_ids"] = planned_question_ids
    clean["assigned_question_ids"] = planned_question_ids
    clean["planned_question_ids"] = planned_question_ids
    clean["cross_question_ids"] = []
    clean["question_assignments"] = [
        item for item in assignments if str(item.get("question_id") or "") in planned_question_ids
    ]
    clean["status"] = "planned"
    return clean


def _eval_readiness_report(
    *,
    source: GenerationRun | None,
    workspace: dict[str, Any] | None,
) -> dict[str, Any]:
    issues: list[str] = []
    if source is None:
        issues.append("source run not found")
    if workspace is None:
        issues.append("source run has no triage workspace")
        return {"ready": False, "issues": issues}
    source_order = [str(value) for value in workspace.get("source_order") or []]
    source_sets = [item for item in workspace.get("source_sets") or [] if isinstance(item, dict)]
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    if not source_order:
        issues.append("workspace has no pinned source order")
    if not source_sets:
        issues.append("workspace has no source sets")
    if not questions:
        issues.append("workspace has no coverage questions")
    planner_workspace = {
        **workspace,
        "source_sets": [_clean_planned_set(item) for item in source_sets],
    }
    issues.extend(triage_plan_violations(planner_workspace))
    return {
        "ready": not issues,
        "issues": issues,
        "source_run_id": str(source.id) if source is not None else "",
        "source_count": len(source_order),
        "set_count": len(source_sets),
        "question_count": len(questions),
        "first_set_id": str((source_sets[0] if source_sets else {}).get("set_id") or ""),
        "first_set_source_count": len((source_sets[0] if source_sets else {}).get("source_refs") or []),
    }


def _append_readiness_issue(report: dict[str, Any], issue: str) -> dict[str, Any]:
    issues = list(report.get("issues") or [])
    if issue not in issues:
        issues.append(issue)
    return {**report, "ready": False, "issues": issues}


def _append_golden_preflight_issues(
    report: dict[str, Any],
    *,
    workspace: dict[str, Any],
    source_manifest_hash: str,
    mode: str,
    expected_manifest_hash: str,
    expected_source_refs: list[str],
    expected_salient_facts: list[dict[str, Any]],
    expected_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    if expected_manifest_hash and source_manifest_hash != expected_manifest_hash:
        report = _append_readiness_issue(
            report,
            "golden corpus manifest mismatch: "
            f"expected {expected_manifest_hash}, got {source_manifest_hash or '<missing>'}",
        )
    source_order = {str(value) for value in workspace.get("source_order") or [] if str(value)}
    expected_refs_from_expectations = {
        str(item.get("source_ref") or "")
        for item in [*expected_salient_facts, *expected_gaps]
        if str(item.get("source_ref") or "")
    }
    missing_source_refs = sorted((set(expected_source_refs) | expected_refs_from_expectations) - source_order)
    if missing_source_refs:
        report = _append_readiness_issue(
            report,
            "golden corpus missing source_refs: " + ", ".join(missing_source_refs),
        )
    question_ids = {
        str(item.get("question_id") or "")
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict) and str(item.get("question_id") or "")
    }
    expected_question_ids = {
        str(item.get("question_id") or "")
        for item in [*expected_salient_facts, *expected_gaps]
        if str(item.get("question_id") or "")
    }
    missing_question_ids = sorted(expected_question_ids - question_ids)
    if missing_question_ids:
        report = _append_readiness_issue(
            report,
            "golden expectations reference unknown questions: " + ", ".join(missing_question_ids),
        )
    if mode == TRIAGE_EVAL_STOP_FIRST_SET:
        first_set = next(
            (item for item in workspace.get("source_sets") or [] if isinstance(item, dict)),
            {},
        )
        first_set_refs = {str(value) for value in first_set.get("source_refs") or [] if str(value)}
        expected_refs = {
            str(item.get("source_ref") or "")
            for item in [*expected_salient_facts, *expected_gaps]
            if str(item.get("source_ref") or "")
        }
        outside_first_set = sorted(expected_refs - first_set_refs)
        if outside_first_set:
            report = _append_readiness_issue(
                report,
                "first_set golden expectations reference sources outside first set: "
                + ", ".join(outside_first_set),
            )
    return report


def _first_set_structural_report(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> dict[str, Any]:
    return _source_set_structural_report(workspace, source_set)


def _source_set_structural_report(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    source_refs = {str(value) for value in source_set.get("source_refs") or []}
    assessments = [
        item
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    findings = [
        item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    hints = [
        item
        for item in workspace.get("coverage_hints") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    issues: list[str] = []
    analyzed_refs = {str(item.get("source_ref") or "") for item in assessments}
    if analyzed_refs != source_refs:
        issues.append("not every first-set source has exactly one current analysis")
    unresolved_assessments = [
        str(item.get("source_ref") or "")
        for item in assessments
        if not item.get("finding_ids") and not str(item.get("no_findings_reason") or "").strip()
    ]
    if unresolved_assessments:
        issues.append("source analyses lack findings or no_findings_reason")
    ungrounded_findings = [
        str(item.get("finding_id") or "") for item in findings if not item.get("evidence_ids")
    ]
    if ungrounded_findings:
        issues.append("findings lack materialized evidence")
    expected_question_ids = {str(value) for value in source_set.get("question_ids") or []}
    legacy_outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    if legacy_outcomes:
        issues.append("set-agent wrote legacy coverage outcomes")
    return {
        "passed": not issues,
        "issues": issues,
        "source_count": len(source_refs),
        "assessment_count": len(assessments),
        "finding_count": len(findings),
        "question_count": len(expected_question_ids),
        "coverage_hint_count": len(hints),
        "legacy_outcome_count": len(legacy_outcomes),
    }


def _full_triage_structural_report(
    *,
    workspace: dict[str, Any],
    triage_state: Any,
) -> dict[str, Any]:
    issues: list[str] = []
    source_sets = [item for item in workspace.get("source_sets") or [] if isinstance(item, dict)]
    source_order = [str(value) for value in workspace.get("source_order") or []]
    terminal_sets = [
        item for item in source_sets if str(item.get("status") or "") in {"mapped", "deferred"}
    ]
    if len(terminal_sets) != len(source_sets):
        issues.append("not every source set reached mapped/deferred")
    assessments = [item for item in workspace.get("source_assessments") or [] if isinstance(item, dict)]
    assessed_refs = [str(item.get("source_ref") or "") for item in assessments]
    if sorted(assessed_refs) != sorted(source_order) or len(assessed_refs) != len(set(assessed_refs)):
        issues.append("not every source has exactly one current assessment")
    if getattr(triage_state, "source_map", None) is None:
        issues.append("source map was not materialized")
    if getattr(triage_state, "corpus_brief", None) is None:
        issues.append("corpus brief / research handoff was not materialized")
    triage_gate = getattr(triage_state, "triage_gate", None)
    if triage_gate is not None and getattr(triage_gate, "passed", None) is not True:
        issues.append("source triage gate did not pass")
    corpus_brief = getattr(triage_state, "corpus_brief", None)
    handoff_count = len(getattr(corpus_brief, "research_handoff", []) or []) if corpus_brief is not None else 0
    return {
        "passed": not issues,
        "issues": issues,
        "source_count": len(source_order),
        "source_set_count": len(source_sets),
        "terminal_source_set_count": len(terminal_sets),
        "assessment_count": len(assessments),
        "finding_count": len(workspace.get("source_findings") or []),
        "coverage_hint_count": len(workspace.get("coverage_hints") or []),
        "coverage_synthesis_count": len(workspace.get("coverage_syntheses") or []),
        "research_handoff_count": handoff_count,
    }


def _source_mapping_semantic_expectation_report(
    workspace: dict[str, Any],
    *,
    expected_salient_facts: list[dict[str, Any]],
    expected_gaps: list[dict[str, Any]],
    research_handoff: list[Any] | None = None,
) -> dict[str, Any]:
    expectations = [*expected_salient_facts, *expected_gaps]
    if not expectations:
        return {
            "passed": False,
            "issues": ["source-mapping semantic postflight requires explicit expectations"],
            "reports": [],
        }
    source_sets = [item for item in workspace.get("source_sets") or [] if isinstance(item, dict)]
    source_set_by_ref: dict[str, dict[str, Any]] = {}
    for source_set in source_sets:
        for source_ref in source_set.get("source_refs") or []:
            source_set_by_ref[str(source_ref)] = source_set
    reports: list[dict[str, Any]] = []
    issues: list[str] = []
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for fact in expected_salient_facts:
        source_ref = str(fact.get("source_ref") or "")
        source_set = source_set_by_ref.get(source_ref)
        if source_set is None:
            issues.append(f"expected salient fact source_ref not in any source set: {source_ref}")
            continue
        set_id = str(source_set.get("set_id") or "")
        grouped.setdefault(set_id, {"facts": [], "gaps": []})["facts"].append(fact)
    for gap in expected_gaps:
        source_ref = str(gap.get("source_ref") or "")
        source_set = source_set_by_ref.get(source_ref)
        if source_set is None:
            issues.append(f"expected gap source_ref not in any source set: {source_ref}")
            continue
        set_id = str(source_set.get("set_id") or "")
        grouped.setdefault(set_id, {"facts": [], "gaps": []})["gaps"].append(gap)
    source_set_by_id = {str(item.get("set_id") or ""): item for item in source_sets}
    for set_id, group in grouped.items():
        source_set = source_set_by_id.get(set_id)
        if source_set is None:
            continue
        question_ids = sorted(
            {
                str(item.get("question_id") or "")
                for item in [*group["facts"], *group["gaps"]]
                if str(item.get("question_id") or "")
            }
        )
        report = semantic_eval_report(
            workspace=workspace,
            source_set=source_set,
            traces=[],
            question_ids=question_ids,
            expected_question_statuses={},
            expected_requirement_statuses={},
            expected_salient_facts=group["facts"],
            expected_gaps=group["gaps"],
        )
        reports.append(report)
        issues.extend(report["issues"])
    handoff_report = _research_handoff_expectation_report(
        research_handoff or [],
        expected_salient_facts=expected_salient_facts,
    )
    if research_handoff:
        issues.extend(handoff_report["issues"])
    return {"passed": not issues, "issues": issues, "reports": reports, "handoff_report": handoff_report}


def _raise_for_golden_workspace_mismatch(workspace: dict[str, Any], eval_config: dict[str, Any]) -> None:
    expected_manifest_hash = str(eval_config.get("expected_manifest_hash") or "")
    if expected_manifest_hash and str(workspace.get("manifest_hash") or "") != expected_manifest_hash:
        raise RuntimeError(
            "Golden eval corpus manifest mismatch: "
            f"expected {expected_manifest_hash}, got {workspace.get('manifest_hash') or '<missing>'}"
        )
    expected_source_refs = [str(ref) for ref in eval_config.get("expected_source_refs") or [] if str(ref)]
    if expected_source_refs:
        source_refs = {str(ref) for ref in workspace.get("source_order") or [] if str(ref)}
        missing_refs = sorted(set(expected_source_refs) - source_refs)
        if missing_refs:
            raise RuntimeError("Golden eval corpus missing source_refs: " + ", ".join(missing_refs))


def _research_handoff_expectation_report(
    research_handoff: list[Any],
    *,
    expected_salient_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not research_handoff:
        return {"checked": False, "issues": [], "salient_facts": []}
    items = [_handoff_item_dict(item) for item in research_handoff]
    reports: list[dict[str, Any]] = []
    issues: list[str] = []
    for fact in expected_salient_facts:
        label = str(fact.get("label") or "salient_fact")
        question_id = str(fact.get("question_id") or "")
        source_ref = str(fact.get("source_ref") or "")
        required_term_groups = _required_term_groups(fact)
        candidate_findings = [
            finding
            for item in items
            if question_id in {str(value) for value in item.get("question_ids") or []}
            for finding in item.get("findings") or []
            if isinstance(finding, dict) and str(finding.get("source_ref") or "") == source_ref
        ]
        record_missing_terms = [
            _missing_terms([str(finding.get("statement") or "")], required_term_groups)
            for finding in candidate_findings
            if finding.get("evidence_ids")
        ]
        missing_terms = min(record_missing_terms, key=len) if record_missing_terms else _missing_group_labels(
            required_term_groups
        )
        passed = bool(record_missing_terms) and any(not missing for missing in record_missing_terms)
        reports.append(
            {
                "label": label,
                "question_id": question_id,
                "source_ref": source_ref,
                "passed": passed,
                "missing_terms": missing_terms,
            }
        )
        if not passed:
            issues.append(
                f"research handoff missing salient fact {label}"
                + (f"; missing terms: {', '.join(missing_terms)}" if missing_terms else "")
            )
    return {"checked": True, "issues": issues, "salient_facts": reports}


def _handoff_item_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {
        "question_ids": list(getattr(item, "question_ids", []) or []),
        "findings": list(getattr(item, "findings", []) or []),
    }


def _required_term_groups(expectation: dict[str, Any]) -> list[list[str]]:
    raw_groups = expectation.get("required_term_groups") or []
    groups = [
        [str(term).casefold() for term in group if str(term).strip()]
        for group in raw_groups
        if isinstance(group, list)
    ]
    groups = [group for group in groups if group]
    if groups:
        return groups
    return [
        [str(term).casefold()]
        for term in expectation.get("required_terms") or []
        if str(term).strip()
    ]


def _missing_group_labels(required_term_groups: list[list[str]]) -> list[str]:
    return [" / ".join(group) for group in required_term_groups]


def _missing_terms(record: list[str], required_term_groups: list[list[str]]) -> list[str]:
    text = "\n".join(item for item in record if item).casefold()
    return [
        " / ".join(group)
        for group in required_term_groups
        if not any(term in text for term in group)
    ]


def _parse_salient_facts(values: list[str]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for value in values:
        parts = [part.strip() for part in value.split("|")]
        if len(parts) == 4:
            label, source_ref, question_id, terms = parts
            requirement_id = ""
        elif len(parts) == 5:
            label, source_ref, question_id, requirement_id, terms = parts
        else:
            raise ValueError("--expect-salient-fact must use 4 or 5 pipe-separated fields")
        required_terms = [term.strip() for term in terms.split(",") if term.strip()]
        if not label or not source_ref or not question_id or not required_terms:
            raise ValueError("--expect-salient-fact requires label, source_ref, question_id, and terms")
        facts.append(
            {
                "label": label,
                "source_ref": source_ref,
                "question_id": question_id,
                "requirement_id": requirement_id,
                "required_terms": required_terms,
            }
        )
    return facts


def _parse_expected_gaps(values: list[str]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for value in values:
        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 4:
            raise ValueError("--expect-gap must use label|source_ref|question_id|term1,term2")
        label, source_ref, question_id, terms = parts
        required_terms = [term.strip() for term in terms.split(",") if term.strip()]
        if not label or not source_ref or not question_id or not required_terms:
            raise ValueError("--expect-gap requires label, source_ref, question_id, and terms")
        gaps.append(
            {
                "label": label,
                "source_ref": source_ref,
                "question_id": question_id,
                "required_terms": required_terms,
            }
        )
    return gaps


def _golden_expectations(name: str, mode: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not name:
        return [], []
    if name == TRIAGE_EVAL_GOLDEN_HONG_KONG:
        facts = deepcopy(HONG_KONG_GOLDEN_FIRST_SET_SALIENT_FACTS)
        if mode == TRIAGE_EVAL_STOP_FULL_TRIAGE:
            facts.extend(deepcopy(HONG_KONG_GOLDEN_FULL_ONLY_SALIENT_FACTS))
        return facts, deepcopy(HONG_KONG_GOLDEN_GAPS)
    raise ValueError(f"Unknown triage eval golden fixture: {name}")


def _golden_manifest_hash(name: str) -> str:
    return HONG_KONG_GOLDEN_MANIFEST_HASH if name == TRIAGE_EVAL_GOLDEN_HONG_KONG else ""


def _golden_source_refs(name: str, mode: str) -> list[str]:
    if name != TRIAGE_EVAL_GOLDEN_HONG_KONG:
        return []
    if mode == TRIAGE_EVAL_STOP_FULL_TRIAGE:
        return list(HONG_KONG_GOLDEN_FULL_SOURCE_REFS)
    return list(HONG_KONG_GOLDEN_FIRST_SET_SOURCE_REFS)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run clean V2 source-triage evals from a pinned source run plan.")
    run_selector = parser.add_mutually_exclusive_group(required=True)
    run_selector.add_argument("--source-run-id", type=uuid.UUID)
    run_selector.add_argument("--resume-run-id", type=uuid.UUID)
    parser.add_argument(
        "--mode",
        choices=[TRIAGE_EVAL_STOP_FIRST_SET, TRIAGE_EVAL_STOP_FULL_TRIAGE],
        default=TRIAGE_EVAL_STOP_FIRST_SET,
    )
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--golden",
        choices=[TRIAGE_EVAL_GOLDEN_HONG_KONG],
        default="",
        help="Append fixed semantic expectations for a known golden corpus.",
    )
    parser.add_argument(
        "--expect-salient-fact",
        action="append",
        default=[],
        help=(
            "Post-mapper semantic check. Format: label|source_ref|question_id|term1,term2 or "
            "label|source_ref|question_id|requirement_id|term1,term2"
        ),
    )
    parser.add_argument(
        "--expect-gap",
        action="append",
        default=[],
        help="Post-mapper semantic check. Format: label|source_ref|question_id|term1,term2",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    max_allowed_cost = (
        TRIAGE_FULL_TRIAGE_EVAL_MAX_COST_USD
        if args.mode == TRIAGE_EVAL_STOP_FULL_TRIAGE
        else TRIAGE_FIRST_SET_EVAL_MAX_COST_USD
    )
    max_cost_usd = float(args.max_cost_usd if args.max_cost_usd is not None else max_allowed_cost)
    if max_cost_usd <= 0 or max_cost_usd > max_allowed_cost:
        raise SystemExit(
            f"--max-cost-usd must be within (0, {max_allowed_cost:.2f}] for mode={args.mode}"
        )
    golden_facts, golden_gaps = _golden_expectations(args.golden, args.mode)
    expected_salient_facts = [*golden_facts, *_parse_salient_facts(args.expect_salient_fact)]
    expected_gaps = [*golden_gaps, *_parse_expected_gaps(args.expect_gap)]
    expected_manifest_hash = _golden_manifest_hash(args.golden)
    expected_source_refs = _golden_source_refs(args.golden, args.mode)
    try:
        if args.preflight_only:
            if args.source_run_id is None:
                raise ValueError("--preflight-only requires --source-run-id")
            result = asyncio.run(
                preflight_first_set_eval(
                    args.source_run_id,
                    mode=args.mode,
                    expected_manifest_hash=expected_manifest_hash,
                    expected_source_refs=expected_source_refs,
                    expected_salient_facts=expected_salient_facts,
                    expected_gaps=expected_gaps,
                )
            )
        elif args.resume_run_id is not None:
            if args.mode == TRIAGE_EVAL_STOP_FULL_TRIAGE:
                result = asyncio.run(
                    resume_full_triage_eval(
                        eval_run_id=args.resume_run_id,
                        max_cost_usd=max_cost_usd,
                        poll_seconds=max(0.1, args.poll_seconds),
                    )
                )
            else:
                result = asyncio.run(
                    resume_first_set_eval(
                        eval_run_id=args.resume_run_id,
                        max_cost_usd=max_cost_usd,
                        poll_seconds=max(0.1, args.poll_seconds),
                    )
                )
        else:
            assert args.source_run_id is not None
            if args.mode == TRIAGE_EVAL_STOP_FULL_TRIAGE:
                result = asyncio.run(
                    run_full_triage_eval(
                        source_run_id=args.source_run_id,
                        max_cost_usd=max_cost_usd,
                        expected_salient_facts=expected_salient_facts,
                        expected_gaps=expected_gaps,
                        expected_manifest_hash=expected_manifest_hash,
                        expected_source_refs=expected_source_refs,
                        poll_seconds=max(0.1, args.poll_seconds),
                    )
                )
            else:
                result = asyncio.run(
                    run_first_set_eval(
                        source_run_id=args.source_run_id,
                        max_cost_usd=max_cost_usd,
                        expected_salient_facts=expected_salient_facts,
                        expected_gaps=expected_gaps,
                        expected_manifest_hash=expected_manifest_hash,
                        expected_source_refs=expected_source_refs,
                        poll_seconds=max(0.1, args.poll_seconds),
                    )
                )
    except (KeyboardInterrupt, asyncio.CancelledError):
        _emit("eval_cancelled")
        return
    except Exception as exc:
        _emit("eval_failed", error_type=type(exc).__name__, error=str(exc))
        raise SystemExit(1) from None
    _emit("eval_completed", **result)


if __name__ == "__main__":
    main()


__all__ = [
    "EvalBudgetExceeded",
    "preflight_first_set_eval",
    "resume_first_set_eval",
    "resume_full_triage_eval",
    "run_full_triage_eval",
    "run_first_set_eval",
]
