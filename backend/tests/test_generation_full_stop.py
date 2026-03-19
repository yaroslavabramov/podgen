from __future__ import annotations

import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Update

from app.models.generation_run import GenerationStatus
from app.pipeline.step_control import StepExecutionStopped, assert_step_not_stopped
from app.workers.generation_tasks import _run_pipeline_async


class _FakeResult:
    def __init__(self, scalar=None, *, rowcount: int | None = None) -> None:
        self._scalar = scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar


class _FakeWorkerDB:
    def __init__(self, run_or_config, *, update_rowcount: int = 1, refreshed_run=None) -> None:
        self.run_or_config = run_or_config
        self.update_rowcount = update_rowcount
        self.refreshed_run = refreshed_run if refreshed_run is not None else run_or_config
        self.commit_calls = 0
        self._select_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        if isinstance(statement, Update):
            if self.update_rowcount and hasattr(self.run_or_config, "status"):
                self.run_or_config.status = GenerationStatus.RUNNING
            return _FakeResult(rowcount=self.update_rowcount)

        self._select_calls += 1
        if self._select_calls == 1:
            return _FakeResult(self.run_or_config)
        return _FakeResult(self.refreshed_run)

    async def commit(self) -> None:
        self.commit_calls += 1


def _full_run(*, stop_requested: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=GenerationStatus.QUEUED,
        config={
            "execution_mode": "full",
            "language": "en",
            "stop_requested": stop_requested,
            "stop_requested_at": "2026-03-19T10:00:00" if stop_requested else None,
            "stop_acknowledged_at": None,
            "stop_reason": "user_requested_stop" if stop_requested else None,
        },
        langgraph_thread_id=str(uuid.uuid4()),
        started_at=None,
        current_phase="phase3_research",
        current_step="research_fast_web",
        completed_at=None,
        error_message=None,
    )


@pytest.mark.asyncio
async def test_assert_step_not_stopped_honors_full_mode_stop_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeWorkerDB(
        {
            "execution_mode": "full",
            "stop_requested": True,
            "stop_reason": "user_requested_stop",
        }
    )

    monkeypatch.setattr(
        "app.pipeline.step_control.current_trace_context",
        lambda: {"generation_run_id": str(uuid.uuid4()), "execution_mode": "full"},
    )
    monkeypatch.setattr("app.pipeline.step_control.worker_session_factory", lambda: db)

    with pytest.raises(StepExecutionStopped, match="Generation execution stopped by user request."):
        await assert_step_not_stopped()


@pytest.mark.asyncio
async def test_run_pipeline_stop_during_execution_marks_run_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _full_run(stop_requested=False)
    db = _FakeWorkerDB(run)
    published = []

    async def _fake_execute_pipeline(*_args, **_kwargs):
        raise StepExecutionStopped("Generation execution stopped by user request.")

    async def _no_stop():
        return None

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.graph.execute_pipeline", _fake_execute_pipeline)
    monkeypatch.setattr("app.pipeline.step_control.assert_step_not_stopped", _no_stop)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda run_id, event: published.append((run_id, event)))
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_pipeline_async(str(run.id), resume=False)

    assert run.status == GenerationStatus.CANCELLED
    assert run.error_message is None
    assert run.completed_at is not None
    assert run.config["stop_requested"] is False
    assert isinstance(run.config["stop_acknowledged_at"], str)
    assert run.config["stop_reason"] == "Generation execution stopped by user request."
    assert db.commit_calls == 2

    assert len(published) == 1
    _, event = published[0]
    assert event.type == "generation_cancelled"
    assert event.data["reason"] == "Generation execution stopped by user request."


@pytest.mark.asyncio
async def test_run_pipeline_queued_cancel_race_does_not_restart_cancelled_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_run = _full_run(stop_requested=False)
    cancelled_run = _full_run(stop_requested=False)
    cancelled_run.id = stale_run.id
    cancelled_run.project_id = stale_run.project_id
    cancelled_run.user_id = stale_run.user_id
    cancelled_run.status = GenerationStatus.CANCELLED
    cancelled_run.completed_at = None
    cancelled_run.config["stop_requested"] = False
    db = _FakeWorkerDB(stale_run, update_rowcount=0, refreshed_run=cancelled_run)

    async def _should_not_execute(*_args, **_kwargs):
        raise AssertionError("pipeline should not execute after queued cancel wins")

    async def _no_stop():
        return None

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.graph.execute_pipeline", _should_not_execute)
    monkeypatch.setattr("app.pipeline.step_control.assert_step_not_stopped", _no_stop)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_pipeline_async(str(stale_run.id), resume=False)

    assert stale_run.status == GenerationStatus.QUEUED
    assert cancelled_run.status == GenerationStatus.CANCELLED
    assert db.commit_calls == 0


@pytest.mark.asyncio
async def test_run_pipeline_post_claim_cancel_race_does_not_execute_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_run = _full_run(stop_requested=False)
    cancelled_run = _full_run(stop_requested=False)
    cancelled_run.id = stale_run.id
    cancelled_run.project_id = stale_run.project_id
    cancelled_run.user_id = stale_run.user_id
    cancelled_run.status = GenerationStatus.CANCELLED
    cancelled_run.completed_at = None
    cancelled_run.config["stop_requested"] = False
    db = _FakeWorkerDB(stale_run, update_rowcount=1, refreshed_run=cancelled_run)

    async def _should_not_execute(*_args, **_kwargs):
        raise AssertionError("pipeline should not execute after post-claim cancel wins")

    async def _no_stop():
        return None

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.graph.execute_pipeline", _should_not_execute)
    monkeypatch.setattr("app.pipeline.step_control.assert_step_not_stopped", _no_stop)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_pipeline_async(str(stale_run.id), resume=False)

    assert stale_run.status == GenerationStatus.RUNNING
    assert cancelled_run.status == GenerationStatus.CANCELLED
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_run_pipeline_gap_after_refresh_honors_persisted_stop_before_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _full_run(stop_requested=False)
    db = _FakeWorkerDB(run, update_rowcount=1, refreshed_run=run)

    async def _should_not_execute(*_args, **_kwargs):
        raise AssertionError("pipeline should not execute when stop lands in pre-execute gap")

    async def _stop_in_gap():
        raise StepExecutionStopped("Generation execution stopped by user request.")

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.graph.execute_pipeline", _should_not_execute)
    monkeypatch.setattr("app.pipeline.step_control.assert_step_not_stopped", _stop_in_gap)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_pipeline_async(str(run.id), resume=False)

    assert run.status == GenerationStatus.CANCELLED
    assert run.config["stop_requested"] is False
    assert run.config["stop_reason"] == "Generation execution stopped by user request."
