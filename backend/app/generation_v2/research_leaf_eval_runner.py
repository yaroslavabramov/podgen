from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import uuid
from typing import Any

from app.database import async_session_factory
from app.generation_v2.agents.research_memo_agent import run_research_job
from app.generation_v2.artifact_costs import summarize_tool_trace_costs
from app.generation_v2.pipeline.triage_state_loader import load_passed_triage_state
from app.generation_v2.research_leaf_eval_clone import (
    clone_research_leaf_eval_run as _clone_research_leaf_eval_run,
    research_leaf_trace_metrics as _research_leaf_trace_metrics,
    rewrite_clone_payload as _rewrite_clone_payload,
)
from app.generation_v2.research_leaf_eval_contract import (
    RESEARCH_LEAF_EVAL_DEFAULT_MODEL,
    RESEARCH_LEAF_EVAL_DEFAULT_REASONING_EFFORT,
    RESEARCH_LEAF_EVAL_MAX_COST_USD,
    ResearchLeafEvalCostGuard,
    ResearchLeafEvalRuntime,
    validate_eval_model_and_effort as _validate_eval_model_and_effort,
)
from app.generation_v2.research_leaf_eval_report import (
    answered_requirement_atom_check as _answered_requirement_atom_check,
    job_for_question as _job_for_question,
    job_prompt_size_report as _job_prompt_size_report,
    job_report_evidence_ids as _job_report_evidence_ids,
    leaf_job_structural_report as _leaf_job_structural_report,
    load_evidence_text_by_id as _load_evidence_text_by_id,
)
from app.generation_v2.research_workspace import (
    ensure_research_workspace,
    save_research_workspace,
    source_map_research_summary,
)
from app.generation_v2.triage_eval_support import emit as _emit
from app.models.generation_run import GenerationRun, GenerationStatus


__all__ = [
    "RESEARCH_LEAF_EVAL_DEFAULT_MODEL",
    "RESEARCH_LEAF_EVAL_DEFAULT_REASONING_EFFORT",
    "RESEARCH_LEAF_EVAL_MAX_COST_USD",
    "ResearchLeafEvalCostGuard",
    "ResearchLeafEvalRuntime",
    "_answered_requirement_atom_check",
    "_clone_research_leaf_eval_run",
    "_job_for_question",
    "_job_prompt_size_report",
    "_job_report_evidence_ids",
    "_leaf_job_structural_report",
    "_load_evidence_text_by_id",
    "_research_leaf_trace_metrics",
    "_rewrite_clone_payload",
    "_validate_eval_model_and_effort",
    "preflight_leaf_research_eval",
    "run_leaf_research_eval",
]


async def preflight_leaf_research_eval(
    *,
    triage_run_id: uuid.UUID,
    question_id: str,
    model: str = RESEARCH_LEAF_EVAL_DEFAULT_MODEL,
    reasoning_effort: str = RESEARCH_LEAF_EVAL_DEFAULT_REASONING_EFFORT,
) -> dict[str, Any]:
    try:
        model, reasoning_effort = _validate_eval_model_and_effort(model, reasoning_effort)
    except ValueError as exc:
        return {"ready": False, "issues": [str(exc)]}
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, triage_run_id)
        if run is None:
            return {"ready": False, "issues": ["triage run not found"]}
        run_id = run.id
        try:
            triage_state = await load_passed_triage_state(db, run)
        except Exception as exc:
            return {"ready": False, "issues": [f"triage state is not loadable: {exc}"]}
        if triage_state.corpus_brief is None:
            return {"ready": False, "issues": ["triage run has no CorpusBrief"]}
        workspace = await ensure_research_workspace(
            db,
            run=run,
            source_map=triage_state.source_map,
            source_inventory=triage_state.source_inventory,
            run_view=triage_state.corpus.run_view,
            corpus_brief=triage_state.corpus_brief,
            corpus_brief_artifact_id=triage_state.corpus_brief_artifact_id,
        )
        job = _job_for_question(workspace, question_id)
        if job is None:
            await db.rollback()
            return {"ready": False, "issues": [f"no leaf research job for question_id={question_id}"]}
        job_status = str(job.get("status") or "")
        if job_status not in {"planned", "in_progress"}:
            await db.rollback()
            return {
                "ready": False,
                "issues": [f"leaf research job is already terminal: {job.get('job_id')}={job_status}"],
                "run_id": str(run_id),
                "question_id": question_id,
                "requested_model": model,
                "requested_reasoning_effort": reasoning_effort,
                "job_id": job.get("job_id"),
                "job_status": job_status,
            }
        cost = await summarize_tool_trace_costs(db, run_id)
        report = _job_prompt_size_report(
            workspace,
            job,
            source_map_summary=source_map_research_summary(
                triage_state.source_map,
                triage_state.source_inventory,
                include_evidence_ids=True,
            ),
        )
        await db.rollback()
        return {
            "ready": True,
            "issues": [],
            "run_id": str(run_id),
            "question_id": question_id,
            "requested_model": model,
            "requested_reasoning_effort": reasoning_effort,
            "job_id": job.get("job_id"),
            "job_status": job_status,
            "baseline_cost": cost["totals"],
            **report,
        }


async def run_leaf_research_eval(
    *,
    triage_run_id: uuid.UUID,
    question_id: str,
    max_cost_usd: float,
    model: str = RESEARCH_LEAF_EVAL_DEFAULT_MODEL,
    reasoning_effort: str = RESEARCH_LEAF_EVAL_DEFAULT_REASONING_EFFORT,
) -> dict[str, Any]:
    if max_cost_usd <= 0 or max_cost_usd > RESEARCH_LEAF_EVAL_MAX_COST_USD:
        raise ValueError(f"max_cost_usd must be within (0, {RESEARCH_LEAF_EVAL_MAX_COST_USD:.2f}]")
    model, reasoning_effort = _validate_eval_model_and_effort(model, reasoning_effort)
    preflight = await preflight_leaf_research_eval(
        triage_run_id=triage_run_id,
        question_id=question_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    _emit("research_leaf_eval_preflight", **preflight)
    if preflight["ready"] is not True:
        raise RuntimeError("Research leaf eval preflight failed: " + "; ".join(preflight["issues"]))
    eval_run = await _clone_research_leaf_eval_run(
        source_run_id=triage_run_id,
        question_id=question_id,
        max_cost_usd=max_cost_usd,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    try:
        return await _execute_leaf_research_eval_run(
            eval_run_id=eval_run.id,
            source_run_id=triage_run_id,
            question_id=question_id,
            max_cost_usd=max_cost_usd,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    except Exception as exc:
        await _mark_leaf_eval_run_finished(eval_run.id, status=GenerationStatus.FAILED, error=str(exc))
        raise


async def _execute_leaf_research_eval_run(
    *,
    eval_run_id: uuid.UUID,
    source_run_id: uuid.UUID,
    question_id: str,
    max_cost_usd: float,
    model: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, eval_run_id)
        if run is None:
            raise RuntimeError("research leaf eval run disappeared before execution")
        run_id = run.id
        triage_state = await load_passed_triage_state(db, run)
        if triage_state.corpus_brief is None:
            raise RuntimeError("eval run has no CorpusBrief")
        workspace = await ensure_research_workspace(
            db,
            run=run,
            source_map=triage_state.source_map,
            source_inventory=triage_state.source_inventory,
            run_view=triage_state.corpus.run_view,
            corpus_brief=triage_state.corpus_brief,
            corpus_brief_artifact_id=triage_state.corpus_brief_artifact_id,
        )
        job = _job_for_question(workspace, question_id)
        if job is None:
            raise RuntimeError(f"no leaf research job for question_id={question_id}")
        if str(job.get("status") or "") not in {"planned", "in_progress"}:
            raise RuntimeError(f"leaf research job is already terminal: {job.get('job_id')}={job.get('status')}")
        original_workspace = deepcopy(workspace)
        baseline_cost = await summarize_tool_trace_costs(db, run_id)
        baseline_cost_usd = float(baseline_cost["totals"].get("estimated_token_cost_usd") or 0.0)
        runtime = ResearchLeafEvalRuntime(
            model_override=model,
            reasoning_effort=reasoning_effort,
            before_provider_call=ResearchLeafEvalCostGuard(
                max_delta_cost_usd=max_cost_usd,
                baseline_cost_usd=baseline_cost_usd,
            ),
        )
        workspace = await run_research_job(
            db=db,
            run=run,
            runtime=runtime,
            workspace=workspace,
            job_id=str(job.get("job_id") or ""),
            source_map=triage_state.source_map,
            source_inventory=triage_state.source_inventory,
            source_map_summary=source_map_research_summary(
                triage_state.source_map,
                triage_state.source_inventory,
                include_evidence_ids=True,
            ),
            evidence_tools=triage_state.evidence_tools,
            evidence_toolset=triage_state.evidence_toolset,
        )
        job = _job_for_question(workspace, question_id) or {}
        if str(job.get("status") or "") in {"planned", "in_progress"}:
            raise RuntimeError(f"leaf research job did not finish durably: {job.get('job_id')}")
        evidence_text_by_id = await _load_evidence_text_by_id(
            db,
            run_id=run_id,
            evidence_ids=_job_report_evidence_ids(job),
        )
        cost = await summarize_tool_trace_costs(db, run_id)
        total_cost_usd = float(cost["totals"].get("estimated_token_cost_usd") or 0.0)
        trace_metrics = await _research_leaf_trace_metrics(db, run_id)
        structural_report = _leaf_job_structural_report(
            workspace,
            job,
            evidence_text_by_id=evidence_text_by_id,
        )
        result = {
            "run_id": str(run_id),
            "source_run_id": str(source_run_id),
            "question_id": question_id,
            "job_id": job.get("job_id"),
            "job_status": job.get("status"),
            "summary": job.get("summary"),
            "requested_model": model,
            "requested_reasoning_effort": reasoning_effort,
            "configured_reasoning_effort": reasoning_effort,
            "observed_models": trace_metrics["observed_models"],
            "model_call_count": trace_metrics["model_call_count"],
            "latency_ms": trace_metrics["latency_ms"],
            "cost": cost["totals"],
            "baseline_cost": baseline_cost["totals"],
            "leaf_delta_cost": {
                "estimated_token_cost_usd": max(0.0, total_cost_usd - baseline_cost_usd),
                "max_cost_usd": max_cost_usd,
            },
            "structural_report": structural_report,
        }
        _emit("research_leaf_eval_completed", **result)
        if not structural_report["passed"]:
            issues = "; ".join(str(issue) for issue in structural_report.get("issues") or [])
            await save_research_workspace(db, generation_run_id=run_id, workspace=original_workspace)
            run.status = GenerationStatus.FAILED
            run.current_step = "research leaf eval failed"
            run.progress_pct = 100
            run.actual_cost = cost
            run.completed_at = datetime.utcnow()
            run.error_message = f"Research leaf eval failed: {issues}"
            await db.commit()
            raise RuntimeError(f"Research leaf eval failed: {issues}")
        await save_research_workspace(db, generation_run_id=run_id, workspace=workspace)
        run.status = GenerationStatus.COMPLETED
        run.current_step = "research leaf eval completed"
        run.progress_pct = 100
        run.actual_cost = cost
        run.completed_at = datetime.utcnow()
        run.error_message = None
        await db.commit()
        return result


async def _mark_leaf_eval_run_finished(run_id: uuid.UUID, *, status: GenerationStatus, error: str = "") -> None:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, run_id)
        if run is None:
            return
        run.status = status
        run.current_step = "research leaf eval completed" if status == GenerationStatus.COMPLETED else "research leaf eval failed"
        run.progress_pct = 100
        run.actual_cost = await summarize_tool_trace_costs(db, run_id)
        run.completed_at = datetime.utcnow()
        run.error_message = error or None
        await db.commit()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one V2 research leaf canary without planner/finalizer/audit.")
    parser.add_argument("--triage-run-id", required=True)
    parser.add_argument("--question-id", required=True)
    parser.add_argument("--max-cost-usd", type=float, default=RESEARCH_LEAF_EVAL_MAX_COST_USD)
    parser.add_argument("--model", default=RESEARCH_LEAF_EVAL_DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=RESEARCH_LEAF_EVAL_DEFAULT_REASONING_EFFORT)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


async def _main_async() -> None:
    args = _parser().parse_args()
    triage_run_id = uuid.UUID(str(args.triage_run_id))
    if args.preflight_only:
        _emit(
            "research_leaf_eval_preflight",
            **await preflight_leaf_research_eval(
                triage_run_id=triage_run_id,
                question_id=str(args.question_id),
                model=str(args.model),
                reasoning_effort=str(args.reasoning_effort),
            ),
        )
        return
    result = await run_leaf_research_eval(
        triage_run_id=triage_run_id,
        question_id=str(args.question_id),
        max_cost_usd=float(args.max_cost_usd),
        model=str(args.model),
        reasoning_effort=str(args.reasoning_effort),
    )
    if not ((result.get("structural_report") or {}).get("passed")):
        raise SystemExit(1)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
