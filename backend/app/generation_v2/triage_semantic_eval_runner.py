from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import uuid
from typing import Any

from sqlalchemy import select

from app.database import async_session_factory
from app.generation_v2.artifact_costs import summarize_tool_trace_costs
from app.generation_v2.pipeline.source_triage_set_flow import run_source_triage_set
from app.generation_v2.pipeline.source_triage_stage import SourceTriageStageRunner
from app.generation_v2.agents.tool_sets import TRIAGE_SEMANTIC_EVAL_TOOL_NAMES
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.source_corpus_run_view import load_current_run_corpus_view
from app.generation_v2.source_triage_workspace import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_state import (
    uncheckpointed_source_read_replays,
)
from app.generation_v2.source_triage_read_checkpointing import (
    pending_comparison_source_refs,
    pending_exact_read_source_refs,
    source_refs_with_reads_newer_than_analysis,
)
from app.generation_v2.triage_eval_support import (
    EvalBudgetExceeded,
    EvalCostGuard,
    emit,
    execute_monitored_eval,
)
from app.generation_v2.triage_semantic_eval_state import (
    build_semantic_eval_workspace,
    semantic_eval_evidence_ids,
)
from app.generation_v2.triage_semantic_eval_evidence import (
    clone_semantic_evidence,
    parsed_evidence_ids,
)
from app.generation_v2.triage_semantic_eval_report import semantic_eval_report
from app.models.generation_run import GenerationRun, GenerationStatus
from app.models.generation_v2 import EvidenceItem, V2ToolTrace


TRIAGE_SEMANTIC_EVAL_MAX_COST_USD = 1.70


async def preflight_semantic_eval(
    *,
    source_run_id: uuid.UUID,
    set_id: str,
    question_ids: list[str],
) -> dict[str, Any]:
    issues: list[str] = []
    async with async_session_factory() as db:
        source = await db.get(GenerationRun, source_run_id)
        workspace = await load_source_triage_workspace(db, source_run_id)
        if source is None:
            issues.append("source run not found")
        if workspace is None:
            issues.append("source run has no triage workspace")
            return {"ready": False, "issues": issues}
        source_set = _workspace_set(workspace, set_id)
        if source_set is None:
            issues.append(f"source set {set_id!r} not found")
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in workspace.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        missing_questions = sorted(set(question_ids) - known_question_ids)
        if missing_questions:
            issues.append("unknown question_ids: " + ", ".join(missing_questions))
        if source_set is not None:
            source_refs = {str(value) for value in source_set.get("source_refs") or []}
            assessed_refs = {
                str(item.get("source_ref") or "")
                for item in workspace.get("source_assessments") or []
                if isinstance(item, dict)
                and str(item.get("set_id") or "") == set_id
                and str(item.get("processing_status") or "resolved") == "resolved"
                and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
            }
            if assessed_refs != source_refs:
                issues.append("frozen set does not have one resolved analysis for every source")
            if uncheckpointed_source_read_replays(workspace, set_id=set_id):
                issues.append("frozen set has uncheckpointed source reads")
            if pending_exact_read_source_refs(workspace, source_set=source_set):
                issues.append("frozen set has pending exact reads")
            if pending_comparison_source_refs(source_set):
                issues.append("frozen set has pending comparisons")
            if source_refs_with_reads_newer_than_analysis(workspace, set_id=set_id):
                issues.append("frozen set has stale source analyses")
        raw = None
        if not issues:
            raw = build_semantic_eval_workspace(
                fresh_workspace=workspace,
                source_workspace=workspace,
                set_id=set_id,
                question_ids=question_ids,
            )
            parsed_ids = parsed_evidence_ids(semantic_eval_evidence_ids(raw))
            evidence = list(
                (
                    await db.execute(
                        select(EvidenceItem).where(EvidenceItem.id.in_(parsed_ids.values()))
                    )
                ).scalars()
            )
            found_ids = {str(item.id) for item in evidence if item.generation_run_id == source_run_id}
            missing_evidence = sorted(set(parsed_ids) - found_ids)
            if missing_evidence:
                issues.append("frozen semantic state has unavailable evidence: " + ", ".join(missing_evidence))
        return {
            "ready": not issues,
            "issues": issues,
            "source_run_id": str(source_run_id),
            "set_id": set_id,
            "question_ids": list(question_ids),
            "source_count": len((source_set or {}).get("source_refs") or []),
            "finding_count": len((raw or {}).get("source_findings") or []),
            "evidence_count": len(semantic_eval_evidence_ids(raw or {})),
        }


async def run_semantic_eval(
    *,
    source_run_id: uuid.UUID,
    set_id: str,
    question_ids: list[str],
    expected_question_statuses: dict[str, str],
    expected_requirement_statuses: dict[tuple[str, str], str],
    max_cost_usd: float,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    if max_cost_usd <= 0 or max_cost_usd > TRIAGE_SEMANTIC_EVAL_MAX_COST_USD:
        raise ValueError(
            f"max_cost_usd must be within (0, {TRIAGE_SEMANTIC_EVAL_MAX_COST_USD:.2f}]"
        )
    readiness = await preflight_semantic_eval(
        source_run_id=source_run_id,
        set_id=set_id,
        question_ids=question_ids,
    )
    emit("semantic_eval_preflight", **readiness)
    if readiness["ready"] is not True:
        raise RuntimeError("Semantic eval preflight failed: " + "; ".join(readiness["issues"]))
    eval_run = await _clone_semantic_eval_run(
        source_run_id=source_run_id,
        set_id=set_id,
        question_ids=question_ids,
        max_cost_usd=max_cost_usd,
    )
    return await execute_monitored_eval(
        run_id=eval_run.id,
        worker_coro=_execute_semantic_eval(
            eval_run_id=eval_run.id,
            source_run_id=source_run_id,
            set_id=set_id,
            question_ids=question_ids,
            expected_question_statuses=expected_question_statuses,
            expected_requirement_statuses=expected_requirement_statuses,
            max_cost_usd=max_cost_usd,
        ),
        max_cost_usd=max_cost_usd,
        poll_seconds=poll_seconds,
    )


async def _clone_semantic_eval_run(
    *,
    source_run_id: uuid.UUID,
    set_id: str,
    question_ids: list[str],
    max_cost_usd: float,
) -> GenerationRun:
    async with async_session_factory() as db:
        source = await db.get(GenerationRun, source_run_id)
        if source is None:
            raise RuntimeError(f"Source generation run {source_run_id} was not found")
        config = dict(source.config or {})
        config["triage_semantic_eval"] = {
            "source_run_id": str(source_run_id),
            "set_id": set_id,
            "question_ids": list(question_ids),
            "input_mode": "frozen_post_reading_state",
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
            current_step="triage semantic eval: preparing frozen state",
            progress_pct=0,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        emit(
            "semantic_eval_started",
            run_id=str(run.id),
            source_run_id=str(source_run_id),
            set_id=set_id,
            max_cost_usd=max_cost_usd,
        )
        return run


async def _execute_semantic_eval(
    *,
    eval_run_id: uuid.UUID,
    source_run_id: uuid.UUID,
    set_id: str,
    question_ids: list[str],
    expected_question_statuses: dict[str, str],
    expected_requirement_statuses: dict[tuple[str, str], str],
    max_cost_usd: float,
) -> dict[str, Any]:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, eval_run_id)
        if run is None:
            raise RuntimeError("Semantic evaluation run disappeared before execution")
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
            raise RuntimeError("Current project corpus differs from the frozen semantic eval corpus")
        source_workspace = await load_source_triage_workspace(db, source_run_id)
        if source_workspace is None:
            raise RuntimeError("Source run has no triage workspace")
        raw_workspace = build_semantic_eval_workspace(
            fresh_workspace=context.workspace,
            source_workspace=source_workspace,
            set_id=set_id,
            question_ids=question_ids,
        )
        evidence_id_map = await clone_semantic_evidence(
            db=db,
            source_run_id=source_run_id,
            target_run=run,
            target_evidence_toolset=context.evidence_toolset,
            evidence_ids=semantic_eval_evidence_ids(raw_workspace),
        )
        workspace = build_semantic_eval_workspace(
            fresh_workspace=context.workspace,
            source_workspace=source_workspace,
            set_id=set_id,
            question_ids=question_ids,
            evidence_id_map=evidence_id_map,
        )
        await save_source_triage_workspace(
            db,
            generation_run_id=run.id,
            workspace=workspace,
        )
        source_set = _workspace_set(workspace, set_id)
        if source_set is None:
            raise RuntimeError("Prepared semantic eval set disappeared")
        emit(
            "semantic_state_ready",
            run_id=str(run.id),
            set_id=set_id,
            source_count=len(source_set.get("source_refs") or []),
            finding_count=len(workspace.get("source_findings") or []),
            evidence_count=len(evidence_id_map),
            question_ids=question_ids,
        )
        completion = await run_source_triage_set(
            run=run,
            db=db,
            runtime=runner.runtime,
            corpus=context.corpus,
            source_inventory=context.source_inventory,
            workspace=workspace,
            source_set=source_set,
            allowed_tool_names=TRIAGE_SEMANTIC_EVAL_TOOL_NAMES,
        )
        workspace = await load_source_triage_workspace(db, run.id) or workspace
        source_set = _workspace_set(workspace, set_id) or source_set
        cost = await summarize_tool_trace_costs(db, run.id)
        observed_cost = float(cost["totals"].get("estimated_token_cost_usd") or 0.0)
        if observed_cost > max_cost_usd:
            raise EvalBudgetExceeded(
                f"Semantic eval exceeded cost ceiling ${max_cost_usd:.2f} at ${observed_cost:.8f}"
            )
        traces = list(
            (
                await db.execute(
                    select(V2ToolTrace)
                    .where(V2ToolTrace.generation_run_id == run.id)
                    .order_by(V2ToolTrace.created_at.asc())
                )
            ).scalars()
        )
        report = semantic_eval_report(
            workspace=workspace,
            source_set=source_set,
            traces=traces,
            question_ids=question_ids,
            expected_question_statuses=expected_question_statuses,
            expected_requirement_statuses=expected_requirement_statuses,
        )
        run.actual_cost = cost
        run.completed_at = datetime.utcnow()
        run.progress_pct = 100
        if report["passed"]:
            run.status = GenerationStatus.COMPLETED
            run.current_step = "triage semantic eval completed"
        else:
            run.status = GenerationStatus.FAILED
            run.current_step = "triage semantic eval failed postflight"
            run.error_message = "; ".join(report["issues"])
        await db.commit()
        result = {
            "run_id": str(run.id),
            "set_id": set_id,
            "completion": completion.model_dump(mode="json"),
            "cost": cost["totals"],
            "semantic_report": report,
        }
        emit("semantic_eval_result", **result)
        if not report["passed"]:
            raise RuntimeError("Semantic eval failed: " + "; ".join(report["issues"]))
        return result


def _workspace_set(workspace: dict[str, Any], set_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in workspace.get("source_sets") or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ),
        None,
    )


def _parse_mapping(values: list[str], *, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, status = value.partition("=")
        if not separator or not key.strip() or not status.strip():
            raise ValueError(f"{label} must use key=status; received {value!r}")
        result[key.strip()] = status.strip()
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one triage set agent on frozen post-reading semantic state."
    )
    parser.add_argument("--source-run-id", type=uuid.UUID, required=True)
    parser.add_argument("--set-id", required=True)
    parser.add_argument("--question-id", action="append", required=True)
    parser.add_argument("--expect-question-status", action="append", default=[])
    parser.add_argument("--expect-requirement-status", action="append", default=[])
    parser.add_argument("--max-cost-usd", type=float, default=TRIAGE_SEMANTIC_EVAL_MAX_COST_USD)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    expected_question_statuses = _parse_mapping(
        args.expect_question_status,
        label="--expect-question-status",
    )
    raw_requirement_statuses = _parse_mapping(
        args.expect_requirement_status,
        label="--expect-requirement-status",
    )
    expected_requirement_statuses: dict[tuple[str, str], str] = {}
    for key, status in raw_requirement_statuses.items():
        question_id, separator, requirement_id = key.partition(":")
        if not separator:
            raise SystemExit("--expect-requirement-status key must be question_id:requirement_id")
        expected_requirement_statuses[(question_id, requirement_id)] = status
    try:
        if args.preflight_only:
            result = asyncio.run(
                preflight_semantic_eval(
                    source_run_id=args.source_run_id,
                    set_id=args.set_id,
                    question_ids=args.question_id,
                )
            )
        else:
            result = asyncio.run(
                run_semantic_eval(
                    source_run_id=args.source_run_id,
                    set_id=args.set_id,
                    question_ids=args.question_id,
                    expected_question_statuses=expected_question_statuses,
                    expected_requirement_statuses=expected_requirement_statuses,
                    max_cost_usd=args.max_cost_usd,
                    poll_seconds=max(0.1, args.poll_seconds),
                )
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        emit("semantic_eval_cancelled")
        return
    emit("semantic_eval_completed", **result)


if __name__ == "__main__":
    main()


__all__ = [
    "TRIAGE_SEMANTIC_EVAL_MAX_COST_USD",
    "preflight_semantic_eval",
    "run_semantic_eval",
    "semantic_eval_report",
]
