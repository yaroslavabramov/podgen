from __future__ import annotations

import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.models.generation_run import GenerationStatus
from app.pipeline.step_control import StepExecutionStopped
from app.workers.generation_tasks import _run_step_async, _run_v2_step_action


class _FakeResult:
    def __init__(self, scalar) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeWorkerDB:
    def __init__(self, run) -> None:
        self.run = run
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        return _FakeResult(self.run)

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _step_run(
    *,
    requested_action: str,
    stop_requested: bool,
    requested_target_node: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=GenerationStatus.QUEUED,
        config={
            "execution_mode": "step",
            "language": "en",
            "step_control": {
                "last_action": requested_action,
                "last_node": "verify_claims",
                "executed_node": "verify_claims",
                "next_node": "research_fast_web",
                "checkpoint_id": "chk-123",
                "is_complete": False,
                "can_rerun": True,
                "available_rerun_nodes": ["research_fast_web", "build_claims", "verify_claims"],
                "requested_action": requested_action,
                "requested_target_node": requested_target_node,
                "requested_at": "2026-03-11T00:00:00",
                "stop_requested": stop_requested,
                "stop_requested_at": "2026-03-11T00:00:01" if stop_requested else None,
                "stop_acknowledged_at": None,
                "stop_reason": "user_requested_stop" if stop_requested else None,
                "updated_at": "2026-03-11T00:00:00",
            },
        },
        langgraph_thread_id=str(uuid.uuid4()),
        started_at=None,
        current_phase="phase4_claims",
        current_step="verify_claims",
        completed_at=None,
        error_message=None,
    )


@pytest.mark.asyncio
async def test_run_step_stop_before_start_preserves_resume_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _step_run(requested_action="next", stop_requested=True)
    db = _FakeWorkerDB(run)
    published = []

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda run_id, event: published.append((run_id, event)))
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_step_async(str(run.id), action="next")

    step_control = run.config["step_control"]
    assert run.status == GenerationStatus.QUEUED
    assert run.current_step == "research_fast_web"
    assert run.current_phase == "phase3_research"
    assert step_control["last_node"] == "verify_claims"
    assert step_control["executed_node"] == "verify_claims"
    assert step_control["next_node"] == "research_fast_web"
    assert step_control["requested_action"] is None
    assert step_control["stop_requested"] is False
    assert step_control["stop_requested_at"] is None
    assert step_control["stop_reason"] == "stopped_before_start"
    assert db.commit_calls == 1

    assert len(published) == 1
    _, event = published[0]
    assert event.type == "generation_paused"
    assert event.data["executed_node"] == "verify_claims"
    assert event.data["next_node"] == "research_fast_web"
    assert event.data["stopped"] is True
    assert event.data["reason"] == "stopped_before_start"


@pytest.mark.asyncio
async def test_run_step_stop_during_execution_resets_step_control_to_interrupted_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _step_run(requested_action="next", stop_requested=False)
    db = _FakeWorkerDB(run)
    published = []

    async def _fake_execute_pipeline_step(*_args, **_kwargs):
        raise StepExecutionStopped("Step execution stopped by user request.")

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.graph.execute_pipeline_step", _fake_execute_pipeline_step)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda run_id, event: published.append((run_id, event)))
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_step_async(str(run.id), action="next")

    step_control = run.config["step_control"]
    assert run.status == GenerationStatus.QUEUED
    assert run.current_step == "research_fast_web"
    assert run.current_phase == "phase3_research"
    assert step_control["last_node"] == "verify_claims"
    assert step_control["executed_node"] == "research_fast_web"
    assert step_control["next_node"] == "research_fast_web"
    assert step_control["checkpoint_id"] == "chk-123"
    assert step_control["requested_action"] is None
    assert step_control["stop_requested"] is False
    assert step_control["stop_requested_at"] is None
    assert step_control["stop_reason"] == "Step execution stopped by user request."
    assert db.commit_calls == 2

    assert len(published) == 1
    _, event = published[0]
    assert event.type == "generation_paused"
    assert event.data["executed_node"] == "research_fast_web"
    assert event.data["next_node"] == "research_fast_web"
    assert event.data["stopped"] is True
    assert event.data["reason"] == "Step execution stopped by user request."


@pytest.mark.asyncio
async def test_run_step_stop_during_rerun_keeps_rerun_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _step_run(requested_action="rerun", stop_requested=False)
    db = _FakeWorkerDB(run)
    published = []

    async def _fake_execute_pipeline_step(*_args, **_kwargs):
        raise StepExecutionStopped("Step execution stopped by user request.")

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.graph.execute_pipeline_step", _fake_execute_pipeline_step)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda run_id, event: published.append((run_id, event)))
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_step_async(str(run.id), action="rerun")

    step_control = run.config["step_control"]
    assert run.status == GenerationStatus.QUEUED
    assert run.current_step == "research_fast_web"
    assert run.current_phase == "phase3_research"
    assert step_control["last_node"] == "verify_claims"
    assert step_control["executed_node"] == "verify_claims"
    assert step_control["next_node"] == "research_fast_web"
    assert step_control["requested_action"] is None
    assert step_control["stop_requested"] is False
    assert step_control["stop_reason"] == "Step execution stopped by user request."

    assert len(published) == 1
    _, event = published[0]
    assert event.type == "generation_paused"
    assert event.data["executed_node"] == "verify_claims"
    assert event.data["next_node"] == "research_fast_web"
    assert event.data["stopped"] is True


@pytest.mark.asyncio
async def test_run_step_stop_during_targeted_rerun_reports_selected_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _step_run(requested_action="rerun", stop_requested=False, requested_target_node="build_claims")
    db = _FakeWorkerDB(run)
    published = []

    async def _fake_execute_pipeline_step(*_args, **_kwargs):
        raise StepExecutionStopped("Step execution stopped by user request.")

    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: db)
    monkeypatch.setattr("app.pipeline.graph.execute_pipeline_step", _fake_execute_pipeline_step)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda run_id, event: published.append((run_id, event)))
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_step_async(str(run.id), action="rerun")

    step_control = run.config["step_control"]
    assert step_control["executed_node"] == "build_claims"
    assert step_control["next_node"] == "research_fast_web"
    assert step_control["requested_target_node"] is None

    assert len(published) == 1
    _, event = published[0]
    assert event.type == "generation_paused"
    assert event.data["executed_node"] == "build_claims"


@pytest.mark.asyncio
async def test_run_v2_step_stop_during_source_triage_pauses_on_same_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=GenerationStatus.FAILED,
        config={
            "generation_mode": "v2",
            "execution_mode": "step",
            "language": "ru",
            "step_control": {
                "last_action": "next",
                "last_node": "corpus_prepare",
                "executed_node": "corpus_prepare",
                "next_node": "source_triage",
                "requested_action": None,
                "requested_target_node": None,
                "stop_requested": False,
            },
        },
        started_at=None,
        current_phase="v2_source_triage",
        current_step="v2_source_triage",
        completed_at=None,
        error_message="previous failed attempt",
    )
    db = _FakeWorkerDB(run)
    published = []
    execute_calls = []

    async def _fake_execute_generation_v2(actual_run, actual_db, *, generate_audio: bool):
        assert (actual_run, actual_db, generate_audio) == (run, db, True)
        execute_calls.append("source_triage")
        config = dict(run.config)
        step_control = dict(config["step_control"])
        step_control["stop_requested"] = True
        step_control["stop_requested_at"] = "2026-07-15T08:10:00"
        config["step_control"] = step_control
        run.config = config
        raise StepExecutionStopped("Step execution stopped by user request.")

    monkeypatch.setattr("app.generation_v2.orchestrator.execute_generation_v2", _fake_execute_generation_v2)
    monkeypatch.setattr("app.realtime.publisher.publish_event", lambda run_id, event: published.append((run_id, event)))
    monkeypatch.setattr("app.workers.generation_tasks.push_trace_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.tracing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("app.workers.generation_tasks.trace_span", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr("app.workers.generation_tasks.safe_set_run", lambda *_args, **_kwargs: None)

    await _run_v2_step_action(
        db,
        run,
        generation_run_id=str(run.id),
        action="next",
        requested_target_node="source_triage",
    )

    step_control = run.config["step_control"]
    assert execute_calls == ["source_triage"]
    assert run.status == GenerationStatus.QUEUED
    assert run.error_message is None
    assert run.completed_at is None
    assert run.current_phase == "v2_source_triage"
    assert run.current_step == "source_triage"
    assert "v2_stop_after_step" not in run.config
    assert step_control["last_node"] == "corpus_prepare"
    assert step_control["executed_node"] == "source_triage"
    assert step_control["next_node"] == "source_triage"
    assert step_control["requested_action"] is None
    assert step_control["stop_requested"] is False
    assert step_control["stop_requested_at"] is None
    assert step_control["stop_reason"] == "Step execution stopped by user request."
    assert db.commit_calls == 2
    assert db.rollback_calls == 1

    assert len(published) == 1
    _, event = published[0]
    assert event.type == "generation_paused"
    assert event.data["executed_node"] == "source_triage"
    assert event.data["next_node"] == "source_triage"
    assert event.data["stopped"] is True
    assert event.data["reason"] == "Step execution stopped by user request."
