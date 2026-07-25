from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from datetime import datetime
import json
import signal
import uuid
from typing import Any

from sqlalchemy import select

from app.database import async_session_factory
from app.generation_v2.artifact_costs import summarize_tool_trace_costs
from app.models.generation_run import GenerationRun, GenerationStatus
from app.models.generation_v2 import V2ToolTrace


TRIAGE_EVAL_CONTEXT_WINDOW_TOKENS = 400_000
TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS = 128_000
TRIAGE_EVAL_ALLOWED_MODELS = {
    "gpt-5.4-mini",
    "gpt-5.4-mini-2026-03-17",
}


class EvalBudgetExceeded(RuntimeError):
    pass


class EvalCostGuard:
    """Stop when another call as costly as the run's observed maximum would cross the ceiling."""

    def __init__(self, *, max_cost_usd: float) -> None:
        self.max_cost_usd = max_cost_usd

    async def __call__(
        self,
        *,
        db: Any,
        generation_run_id: Any,
        requested_model: str,
        max_output_tokens: int | None,
        server_compaction_threshold: int | None,
        **_kwargs: Any,
    ) -> None:
        normalized_model = str(requested_model or "").casefold()
        if normalized_model not in TRIAGE_EVAL_ALLOWED_MODELS:
            raise EvalBudgetExceeded(
                "Triage eval budget contract supports only gpt-5.4-mini; "
                f"received {requested_model or '<missing>'}"
            )
        effective_output_limit = (
            int(max_output_tokens)
            if max_output_tokens is not None
            else TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS
        )
        if effective_output_limit > TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS:
            raise EvalBudgetExceeded("Triage eval output bound exceeds the audited budget contract")
        if (
            server_compaction_threshold is None
            or int(server_compaction_threshold) > TRIAGE_EVAL_CONTEXT_WINDOW_TOKENS
        ):
            raise EvalBudgetExceeded("Triage eval compaction threshold exceeds the model context window")
        report = await summarize_tool_trace_costs(db, generation_run_id)
        observed_cost = float(report["totals"].get("estimated_token_cost_usd") or 0.0)
        reserve = float(report["totals"].get("max_call_cost_usd") or 0.0)
        if observed_cost + reserve > self.max_cost_usd:
            raise EvalBudgetExceeded(
                f"Triage eval stopped before the next provider call: ${observed_cost:.8f} spent, "
                f"${reserve:.8f} observed-call reserve, ${self.max_cost_usd:.2f} ceiling"
            )


async def execute_monitored_eval(
    *,
    run_id: uuid.UUID,
    worker_coro: Coroutine[Any, Any, dict[str, Any]],
    max_cost_usd: float,
    poll_seconds: float,
) -> dict[str, Any]:
    cancelled = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, cancelled.set)
            installed_signals.append(signum)

    worker = asyncio.create_task(worker_coro)
    monitor = asyncio.create_task(
        _monitor_eval(
            run_id=run_id,
            worker=worker,
            cancelled=cancelled,
            max_cost_usd=max_cost_usd,
            poll_seconds=poll_seconds,
        )
    )
    try:
        done, _pending = await asyncio.wait({worker, monitor}, return_when=asyncio.FIRST_COMPLETED)
        if monitor in done:
            monitor_error = monitor.exception()
            if monitor_error is not None:
                await cancel_and_drain(worker)
                raise monitor_error
        result = await worker
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
        return result
    except asyncio.CancelledError:
        await cancel_and_drain(worker)
        await mark_eval_stopped(run_id, status=GenerationStatus.CANCELLED, error="Evaluation cancelled")
        raise
    except Exception as exc:
        await cancel_and_drain(worker)
        await mark_eval_stopped(run_id, status=GenerationStatus.FAILED, error=str(exc))
        raise
    finally:
        cancelled.set()
        if not monitor.done():
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


async def cancel_and_drain(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


async def mark_eval_stopped(
    run_id: uuid.UUID,
    *,
    status: GenerationStatus,
    error: str,
) -> None:
    async with async_session_factory() as db:
        run = await db.get(GenerationRun, run_id)
        if run is None:
            return
        run.status = status
        run.error_message = error
        run.current_step = "triage eval stopped"
        run.actual_cost = await summarize_tool_trace_costs(db, run_id)
        run.completed_at = datetime.utcnow()
        run.v2_worker_task_id = None
        run.v2_worker_claim_token = None
        run.v2_worker_lease_expires_at = None
        await db.commit()


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str), flush=True)


async def _monitor_eval(
    *,
    run_id: uuid.UUID,
    worker: asyncio.Task[Any],
    cancelled: asyncio.Event,
    max_cost_usd: float,
    poll_seconds: float,
) -> None:
    previous: tuple[Any, ...] | None = None
    while not worker.done():
        if cancelled.is_set():
            raise asyncio.CancelledError
        async with async_session_factory() as db:
            report = await summarize_tool_trace_costs(db, run_id)
            errors, blocker = await _latest_diagnostics(db, run_id)
        totals = report["totals"]
        state = (
            int(totals.get("call_count") or 0),
            float(totals.get("estimated_token_cost_usd") or 0.0),
            errors,
            json.dumps(blocker, ensure_ascii=False, sort_keys=True, default=str),
        )
        if state != previous:
            emit(
                "progress",
                run_id=str(run_id),
                calls=state[0],
                cost_usd=state[1],
                error_count=errors,
                blocker=blocker,
            )
            previous = state
        if state[1] >= max_cost_usd:
            raise EvalBudgetExceeded(
                f"Triage eval reached cost ceiling ${max_cost_usd:.2f} at ${state[1]:.8f}"
            )
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


async def _latest_diagnostics(db: Any, run_id: uuid.UUID) -> tuple[int, dict[str, Any] | None]:
    result = await db.execute(
        select(V2ToolTrace)
        .where(V2ToolTrace.generation_run_id == run_id)
        .order_by(V2ToolTrace.created_at.desc())
    )
    errors = 0
    blocker = None
    for trace in result.scalars():
        output = dict(trace.output_json or {})
        if output.get("error"):
            errors += 1
            if blocker is None and str(trace.tool_name) == "finish_triage_source_set":
                blocker = {
                    "error": output.get("error"),
                    "source_blockers": output.get("source_blockers") or [],
                    "missing_source_refs": output.get("missing_source_refs") or [],
                    "missing_question_ids": output.get("missing_question_ids") or [],
                }
    return errors, blocker


__all__ = [
    "EvalBudgetExceeded",
    "EvalCostGuard",
    "TRIAGE_EVAL_ALLOWED_MODELS",
    "TRIAGE_EVAL_CONTEXT_WINDOW_TOKENS",
    "TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS",
    "cancel_and_drain",
    "emit",
    "execute_monitored_eval",
    "mark_eval_stopped",
]
