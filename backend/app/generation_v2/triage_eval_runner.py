from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import uuid
from typing import Any

from app.database import async_session_factory
from app.generation_v2.artifact_costs import summarize_tool_trace_costs
from app.generation_v2.pipeline.source_triage_set_flow import run_source_triage_set
from app.generation_v2.pipeline.source_triage_stage import SourceTriageStageRunner
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.source_corpus import preview_v2_corpus_manifest_hash
from app.generation_v2.source_corpus_run_view import load_current_run_corpus_view
from app.generation_v2.source_triage_workspace import (
    load_source_triage_workspace,
    save_source_triage_workspace,
    unprocessed_source_sets,
)
from app.generation_v2.source_triage_plan_validation import triage_plan_violations
from app.generation_v2.source_triage_question_state import question_outcome_is_current
from app.generation_v2.triage_eval_support import (
    EvalBudgetExceeded,
    EvalCostGuard,
    emit as _emit,
    execute_monitored_eval,
)
from app.models.generation_run import GenerationRun, GenerationStatus


TRIAGE_FIRST_SET_EVAL_MAX_COST_USD = 1.70


async def preflight_first_set_eval(source_run_id: uuid.UUID) -> dict[str, Any]:
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
        return report


async def run_first_set_eval(
    *,
    source_run_id: uuid.UUID,
    max_cost_usd: float,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    if max_cost_usd <= 0 or max_cost_usd > TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:
        raise ValueError(
            f"max_cost_usd must be within (0, {TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:.2f}]"
        )
    readiness = await preflight_first_set_eval(source_run_id)
    _emit("eval_preflight", **readiness)
    if readiness["ready"] is not True:
        raise RuntimeError("Triage eval preflight failed: " + "; ".join(readiness["issues"]))
    eval_run = await _clone_eval_run(source_run_id, max_cost_usd=max_cost_usd)
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
    run, source_run_id = await _prepare_eval_resume(eval_run_id)
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


async def _clone_eval_run(source_run_id: uuid.UUID, *, max_cost_usd: float) -> GenerationRun:
    async with async_session_factory() as db:
        source = await db.get(GenerationRun, source_run_id)
        if source is None:
            raise RuntimeError(f"Source generation run {source_run_id} was not found")
        config = dict(source.config or {})
        config["triage_eval"] = {
            "source_run_id": str(source_run_id),
            "plan_mode": "clean_copy_from_source_run",
            "stop_after": "first_set",
            "max_cost_usd": max_cost_usd,
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
) -> tuple[GenerationRun, uuid.UUID]:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, eval_run_id)
        if run is None:
            raise RuntimeError(f"Evaluation run {eval_run_id} was not found")
        eval_config = dict((run.config or {}).get("triage_eval") or {})
        if eval_config.get("stop_after") != "first_set":
            raise RuntimeError("Run is not a first-set triage evaluation")
        try:
            source_run_id = uuid.UUID(str(eval_config.get("source_run_id") or ""))
        except ValueError as exc:
            raise RuntimeError("Evaluation run has no valid source_run_id") from exc
        workspace = await load_source_triage_workspace(db, run.id)
        if workspace is None or not workspace.get("source_sets"):
            raise RuntimeError("Evaluation run has no persisted set state to resume")
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
        runner = SourceTriageStageRunner(
            run,
            db,
            V2Runtime(before_provider_call=EvalCostGuard(max_cost_usd=max_cost_usd)),
        )
        context = await runner.prepare_context()
        source_view = await load_current_run_corpus_view(db, source_run_id)
        if source_view is None:
            raise RuntimeError("Source run has no pinned corpus view")
        if context.corpus.run_view.manifest_hash != source_view.manifest_hash:
            raise RuntimeError("Current project corpus differs from the source run; refusing a non-comparable eval")
        if resume_existing:
            workspace = await load_source_triage_workspace(db, run.id)
            if workspace is None:
                raise RuntimeError("Evaluation run lost its persisted set state")
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
        completion = await run_source_triage_set(
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
            "completion": completion.model_dump(mode="json"),
            "cost": cost["totals"],
            "structural_report": _first_set_structural_report(
                workspace,
                persisted_set,
            ),
        }
        if result["structural_report"]["passed"] is not True:
            raise RuntimeError(
                "First set failed structural postflight: "
                + "; ".join(result["structural_report"]["issues"])
            )
        _emit("first_set_completed", **result)
        return result


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


def _first_set_structural_report(
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
    outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
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
    current_outcome_ids = {
        question_id
        for item in outcomes
        if (question_id := str(item.get("question_id") or ""))
        and question_outcome_is_current(
            item,
            workspace=workspace,
            source_set=source_set,
            question_id=question_id,
        )
    }
    if current_outcome_ids != expected_question_ids:
        issues.append("first-set questions do not all have current outcomes")
    return {
        "passed": not issues,
        "issues": issues,
        "source_count": len(source_refs),
        "assessment_count": len(assessments),
        "finding_count": len(findings),
        "question_count": len(expected_question_ids),
        "outcome_count": len(outcomes),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a clean V2 planner and exactly one triage source set.")
    run_selector = parser.add_mutually_exclusive_group(required=True)
    run_selector.add_argument("--source-run-id", type=uuid.UUID)
    run_selector.add_argument("--resume-run-id", type=uuid.UUID)
    parser.add_argument("--max-cost-usd", type=float, default=TRIAGE_FIRST_SET_EVAL_MAX_COST_USD)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.max_cost_usd <= 0 or args.max_cost_usd > TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:
        raise SystemExit(
            f"--max-cost-usd must be within (0, {TRIAGE_FIRST_SET_EVAL_MAX_COST_USD:.2f}]"
        )
    try:
        if args.preflight_only:
            if args.source_run_id is None:
                raise ValueError("--preflight-only requires --source-run-id")
            result = asyncio.run(preflight_first_set_eval(args.source_run_id))
        elif args.resume_run_id is not None:
            result = asyncio.run(
                resume_first_set_eval(
                    eval_run_id=args.resume_run_id,
                    max_cost_usd=args.max_cost_usd,
                    poll_seconds=max(0.1, args.poll_seconds),
                )
            )
        else:
            assert args.source_run_id is not None
            result = asyncio.run(
                run_first_set_eval(
                    source_run_id=args.source_run_id,
                    max_cost_usd=args.max_cost_usd,
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
    "run_first_set_eval",
]
