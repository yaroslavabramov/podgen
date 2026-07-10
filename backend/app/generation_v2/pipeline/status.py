from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.contracts.pipeline import V2PipelineStep
from app.models.generation_run import GenerationRun
from app.pipeline.step_control import assert_step_not_stopped
from app.realtime.events import progress, step_completed, step_started, phase_started
from app.realtime.publisher import publish_event, update_generation_progress


class V2StepExecutionStopped(RuntimeError):
    def __init__(self, executed_step: str, next_step: str | None):
        super().__init__(f"V2 stopped after {executed_step}")
        self.executed_step = executed_step
        self.next_step = next_step


def act_step_name(act_number: int) -> str:
    return f"act_{act_number}"


def next_step_after_act(act_index: int, total_acts: int) -> str:
    if act_index < total_acts:
        return act_step_name(act_index + 1)
    return V2PipelineStep.CRITIQUE.value


def act_phase_progress(act_index: int, total_acts: int) -> float:
    return 42 + (act_index - 1) * 28 / max(total_acts, 1)


def act_commit_progress(act_index: int, total_acts: int) -> float:
    return 42 + act_index * 28 / max(total_acts, 1)


async def start_phase(run: GenerationRun, db: AsyncSession, phase: str, message: str, pct: float) -> None:
    await assert_step_not_stopped()
    run.current_phase = phase
    run.current_step = phase
    run.progress_pct = float(pct)
    await db.commit()
    publish_event(str(run.id), phase_started(phase, message))
    publish_event(str(run.id), step_started(phase, phase, message))
    publish_event(str(run.id), progress(float(pct), message))
    update_generation_progress(str(run.id), phase, phase, float(pct))


async def commit_step(
    run: GenerationRun,
    db: AsyncSession,
    step: str | V2PipelineStep,
    pct: float,
    *,
    next_step: str | V2PipelineStep | None,
) -> None:
    await assert_step_not_stopped()
    step_value = str(step)
    next_step_value = str(next_step) if next_step is not None else None
    run.current_step = step_value
    run.progress_pct = float(pct)
    await db.commit()
    phase = run.current_phase or "v2"
    publish_event(str(run.id), step_completed(phase, step_value))
    publish_event(str(run.id), progress(float(pct), step_value.replace("_", " ")))
    update_generation_progress(str(run.id), phase, step_value, float(pct))
    config = run.config or {}
    stop_after_step = str(config.get("v2_stop_after_step") or "") if config.get("execution_mode") == "step" else ""
    if stop_after_step == step_value:
        raise V2StepExecutionStopped(executed_step=step_value, next_step=next_step_value)
