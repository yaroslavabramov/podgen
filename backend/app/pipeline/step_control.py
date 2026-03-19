"""Generation stop signal helpers."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.models.generation_run import GenerationRun
from app.observability import current_trace_context
from app.workers.worker_db import worker_session_factory

logger = logging.getLogger(__name__)


class StepExecutionStopped(RuntimeError):
    """Raised when user requested stop for a running generation node."""


_STOP_MESSAGE = "Step execution stopped by user request."
_FULL_STOP_MESSAGE = "Generation execution stopped by user request."


def _current_run_context() -> tuple[str | None, str]:
    ctx = current_trace_context()
    run_id = ctx.get("generation_run_id")
    execution_mode = str(ctx.get("execution_mode") or "").strip().lower()
    if isinstance(run_id, str) and run_id:
        return run_id, execution_mode
    return None, execution_mode


async def is_step_stop_requested(generation_run_id: str) -> bool:
    """Check persisted stop flag for generation run."""
    try:
        run_uuid = uuid.UUID(generation_run_id)
    except ValueError:
        return False

    async with worker_session_factory() as db:
        result = await db.execute(select(GenerationRun.config).where(GenerationRun.id == run_uuid))
        config = result.scalar_one_or_none()

    if not isinstance(config, dict):
        return False

    if bool(config.get("stop_requested")):
        return True

    step_control = config.get("step_control")
    if isinstance(step_control, dict) and bool(step_control.get("stop_requested")):
        return True

    return False


async def assert_step_not_stopped() -> None:
    """
    Abort costly calls when user requests stop in a running generation.

    Fail-open on signal check errors to avoid turning transient DB issues
    into pipeline crashes.
    """
    run_id, execution_mode = _current_run_context()
    if not run_id:
        return

    try:
        if await is_step_stop_requested(run_id):
            raise StepExecutionStopped(_STOP_MESSAGE if execution_mode == "step" else _FULL_STOP_MESSAGE)
    except StepExecutionStopped:
        raise
    except Exception:
        logger.warning("Failed to check step stop signal; continuing execution", exc_info=True)
