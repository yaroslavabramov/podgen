from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import uuid
from typing import Any

from sqlalchemy import select

from app.database import async_session_factory
from app.generation_v2.artifact_costs import summarize_tool_trace_costs
from app.generation_v2.source_triage_workspace import (
    load_source_triage_workspace,
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
    emit,
    execute_monitored_eval,
)
from app.generation_v2.triage_semantic_eval_state import (
    build_semantic_eval_workspace,
    semantic_eval_evidence_ids,
)
from app.generation_v2.triage_semantic_eval_evidence import parsed_evidence_ids
from app.generation_v2.triage_semantic_eval_report import semantic_eval_report
from app.models.generation_run import GenerationRun, GenerationStatus
from app.models.generation_v2 import EvidenceItem


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
            analysis_refs = {
                str(item.get("source_ref") or "")
                for item in workspace.get("source_analyses") or []
                if isinstance(item, dict)
                and str(item.get("source_ref") or "") in source_refs
                and item.get("current") is not False
                and item.get("accepted") is not False
            }
            if analysis_refs != source_refs:
                issues.append("source-mapping eval requires one current SourceAnalysis for every source")
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
    expected_salient_facts: list[dict[str, Any]] | None = None,
    expected_gaps: list[dict[str, Any]] | None = None,
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
            expected_salient_facts=expected_salient_facts or [],
            expected_gaps=expected_gaps or [],
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
            "input_mode": "source_analysis_postflight",
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
            current_step="triage semantic eval: source analysis postflight",
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
    expected_salient_facts: list[dict[str, Any]],
    expected_gaps: list[dict[str, Any]],
    max_cost_usd: float,
) -> dict[str, Any]:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, eval_run_id)
        if run is None:
            raise RuntimeError("Semantic evaluation run disappeared before execution")
        source_workspace = await load_source_triage_workspace(db, source_run_id)
        if source_workspace is None:
            raise RuntimeError("Source run has no triage workspace")
        source_set = _workspace_set(source_workspace, set_id)
        if source_set is None:
            raise RuntimeError("Semantic eval set disappeared")
        emit(
            "semantic_state_ready",
            run_id=str(run.id),
            set_id=set_id,
            source_count=len(source_set.get("source_refs") or []),
            source_analysis_count=len(source_workspace.get("source_analyses") or []),
            evidence_count=len(semantic_eval_evidence_ids(source_workspace)),
            question_ids=question_ids,
        )
        cost = await summarize_tool_trace_costs(db, run.id)
        report = semantic_eval_report(
            workspace=source_workspace,
            source_set=source_set,
            traces=[],
            question_ids=question_ids,
            expected_question_statuses=expected_question_statuses,
            expected_requirement_statuses=expected_requirement_statuses,
            expected_salient_facts=expected_salient_facts,
            expected_gaps=expected_gaps,
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
            "completion": {
                "ready": report["passed"],
                "summary": "No-model source-mapping semantic postflight.",
                "notes": [],
            },
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run no-model source-mapping semantic postflight on persisted SourceAnalysis state."
    )
    parser.add_argument("--source-run-id", type=uuid.UUID, required=True)
    parser.add_argument("--set-id", required=True)
    parser.add_argument("--question-id", action="append", required=True)
    parser.add_argument("--expect-question-status", action="append", default=[])
    parser.add_argument("--expect-requirement-status", action="append", default=[])
    parser.add_argument(
        "--expect-salient-fact",
        action="append",
        default=[],
        help=(
            "Format: label|source_ref|question_id|term1,term2 or "
            "label|source_ref|question_id|requirement_id|term1,term2"
        ),
    )
    parser.add_argument(
        "--expect-gap",
        action="append",
        default=[],
        help="Format: label|source_ref|question_id|term1,term2",
    )
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
    expected_salient_facts = _parse_salient_facts(args.expect_salient_fact)
    expected_gaps = _parse_expected_gaps(args.expect_gap)
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
                    expected_salient_facts=expected_salient_facts,
                    expected_gaps=expected_gaps,
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
