from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from app.models.generation_run import GenerationRun, GenerationStatus
from app.workers import generation_tasks
from app.workers.celery_app import celery_app
from app.workers.generation_tasks import V2WorkerBusy, _claim_v2_run_for_worker


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _LeaseDb:
    def __init__(self, run: GenerationRun) -> None:
        self.run = run
        self.commits = 0

    async def execute(self, _statement):
        return _Result(self.run)

    async def commit(self) -> None:
        self.commits += 1


def _queued_v2_run() -> GenerationRun:
    return GenerationRun(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=uuid.uuid4(),
        status=GenerationStatus.QUEUED,
        config={"generation_mode": "v2", "execution_mode": "full"},
    )


@pytest.mark.asyncio
async def test_v2_worker_first_delivery_claims_queued_run() -> None:
    run = _queued_v2_run()
    db = _LeaseDb(run)
    now = datetime(2026, 7, 14, 12, 0, 0)

    claim = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-a",
        now=now,
        lease_seconds=120,
    )

    assert claim is not None
    assert run.status == GenerationStatus.RUNNING
    assert run.v2_worker_task_id == "task-a"
    assert run.v2_worker_claim_token == claim.token
    assert run.v2_worker_heartbeat_at == now
    assert run.v2_worker_lease_expires_at == now + timedelta(seconds=120)


@pytest.mark.asyncio
async def test_v2_worker_concurrent_different_task_is_rejected_while_lease_is_live() -> None:
    run = _queued_v2_run()
    db = _LeaseDb(run)
    now = datetime(2026, 7, 14, 12, 0, 0)
    first = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-a",
        now=now,
        lease_seconds=120,
    )
    assert first is not None

    second = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-b",
        now=now + timedelta(seconds=30),
        lease_seconds=120,
    )

    assert isinstance(second, V2WorkerBusy)
    assert run.v2_worker_task_id == "task-a"
    assert run.v2_worker_claim_token == first.token


@pytest.mark.asyncio
async def test_v2_worker_fresh_same_task_redelivery_is_rejected() -> None:
    run = _queued_v2_run()
    db = _LeaseDb(run)
    now = datetime(2026, 7, 14, 12, 0, 0)
    first = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-a",
        now=now,
        lease_seconds=120,
    )
    assert first is not None

    redelivery = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-a",
        now=now + timedelta(seconds=10),
        lease_seconds=120,
    )

    assert isinstance(redelivery, V2WorkerBusy)
    assert run.v2_worker_task_id == "task-a"
    assert run.v2_worker_claim_token == first.token


@pytest.mark.asyncio
@pytest.mark.parametrize("reclaim_reason", ["stale", "expired"])
async def test_v2_worker_same_task_redelivery_reclaims_only_stale_or_expired_owner(
    reclaim_reason: str,
) -> None:
    run = _queued_v2_run()
    db = _LeaseDb(run)
    now = datetime(2026, 7, 14, 12, 0, 0)
    first = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-a",
        now=now,
        lease_seconds=120,
        stale_heartbeat_seconds=60,
    )
    assert first is not None
    if reclaim_reason == "stale":
        run.v2_worker_heartbeat_at = now - timedelta(seconds=61)
        run.v2_worker_lease_expires_at = now + timedelta(seconds=120)
    else:
        run.v2_worker_heartbeat_at = now + timedelta(seconds=120)

    redelivery = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-a",
        now=now + (timedelta(seconds=121) if reclaim_reason == "expired" else timedelta()),
        lease_seconds=120,
        stale_heartbeat_seconds=60,
    )

    assert redelivery is not None
    assert not isinstance(redelivery, V2WorkerBusy)
    assert redelivery.token != first.token
    assert run.v2_worker_claim_token == redelivery.token


@pytest.mark.asyncio
async def test_v2_worker_claims_fresh_sweeper_recovery_delivery() -> None:
    run = _queued_v2_run()
    now = datetime(2026, 7, 14, 12, 0, 0)
    run.status = GenerationStatus.RUNNING
    run.v2_worker_task_id = "recovery-task"
    run.v2_worker_claim_token = generation_tasks._V2_RECOVERY_PENDING_TOKEN
    run.v2_worker_heartbeat_at = now
    run.v2_worker_lease_expires_at = now + timedelta(minutes=2)

    claim = await _claim_v2_run_for_worker(
        _LeaseDb(run),
        run,
        worker_task_id="recovery-task",
        now=now + timedelta(seconds=5),
    )

    assert claim is not None
    assert not isinstance(claim, V2WorkerBusy)
    assert run.v2_worker_claim_token == claim.token


@pytest.mark.asyncio
async def test_v2_worker_expired_lease_can_be_reclaimed_by_new_task() -> None:
    run = _queued_v2_run()
    db = _LeaseDb(run)
    now = datetime(2026, 7, 14, 12, 0, 0)
    first = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-a",
        now=now,
        lease_seconds=60,
    )
    assert first is not None

    reclaimed = await _claim_v2_run_for_worker(
        db,
        run,
        worker_task_id="task-b",
        now=now + timedelta(seconds=61),
        lease_seconds=60,
    )

    assert reclaimed is not None
    assert reclaimed.token != first.token
    assert run.v2_worker_task_id == "task-b"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [GenerationStatus.COMPLETED, GenerationStatus.FAILED, GenerationStatus.CANCELLED],
)
async def test_v2_worker_duplicate_delivery_acks_terminal_run(status: GenerationStatus) -> None:
    run = _queued_v2_run()
    run.status = status
    run.v2_worker_task_id = "old-task"
    run.v2_worker_claim_token = str(uuid.uuid4())
    run.v2_worker_heartbeat_at = datetime(2026, 7, 14, 12, 0, 0)
    run.v2_worker_lease_expires_at = datetime(2026, 7, 14, 12, 15, 0)

    result = await _claim_v2_run_for_worker(
        _LeaseDb(run),
        run,
        worker_task_id="duplicate-task",
        now=datetime(2026, 7, 14, 12, 1, 0),
    )

    assert result is None


def test_v2_worker_busy_delivery_is_retried_instead_of_acked(monkeypatch: pytest.MonkeyPatch) -> None:
    class RetryRequested(Exception):
        pass

    captured: dict[str, object] = {}

    async def busy(*_args, **_kwargs):
        return V2WorkerBusy(retry_after_seconds=37)

    def retry(**kwargs):
        captured.update(kwargs)
        raise RetryRequested

    monkeypatch.setattr(generation_tasks, "_run_generation_v2_async", busy)
    monkeypatch.setattr(generation_tasks.run_generation_v2, "retry", retry)

    with pytest.raises(RetryRequested):
        generation_tasks.run_generation_v2.run(str(uuid.uuid4()))

    assert generation_tasks.run_generation_v2.max_retries is None
    assert captured["countdown"] == 37
    assert "live V2 worker" in str(captured["exc"])
    assert "max_retries" not in captured


def test_redis_visibility_timeout_exceeds_v2_hard_limit() -> None:
    visibility_timeout = celery_app.conf.broker_transport_options["visibility_timeout"]

    assert generation_tasks.run_generation_v2.time_limit is not None
    assert visibility_timeout >= generation_tasks.run_generation_v2.time_limit * 2
    assert celery_app.conf.result_backend_transport_options["visibility_timeout"] == visibility_timeout


@pytest.mark.asyncio
async def test_stale_full_v2_run_is_fenced_and_reenqueued(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _queued_v2_run()
    run.status = GenerationStatus.RUNNING
    run.v2_worker_task_id = "dead-task"
    run.v2_worker_claim_token = str(uuid.uuid4())
    run.v2_worker_heartbeat_at = datetime(2026, 7, 14, 11, 0, 0)
    run.v2_worker_lease_expires_at = datetime(2026, 7, 14, 11, 15, 0)

    class Result:
        def scalars(self):
            return [run]

    statements = []

    class Db:
        async def execute(self, statement):
            statements.append(statement)
            return Result()

        async def commit(self):
            return None

    class Context:
        async def __aenter__(self):
            return Db()

        async def __aexit__(self, *_args):
            return None

    calls: list[dict] = []
    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: Context())
    monkeypatch.setattr(
        generation_tasks.run_generation_v2,
        "apply_async",
        lambda **kwargs: calls.append(kwargs),
    )

    count = await generation_tasks._recover_stale_generation_v2_runs_async(
        now=datetime(2026, 7, 14, 12, 0, 0)
    )

    assert count == 1
    assert run.v2_worker_claim_token == generation_tasks._V2_RECOVERY_PENDING_TOKEN
    assert calls == [
        {
            "args": [str(run.id)],
            "kwargs": {"generate_audio": True},
            "task_id": run.v2_worker_task_id,
        }
    ]
    recovery_sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "generation_mode" in recovery_sql
    assert "execution_mode" in recovery_sql
    assert "compare_parent_run_id" in recovery_sql


def test_stale_recovery_excludes_step_and_compare_children() -> None:
    run = _queued_v2_run()
    assert generation_tasks._is_recoverable_full_v2_run(run) is True

    run.config = {**run.config, "execution_mode": "step"}
    assert generation_tasks._is_recoverable_full_v2_run(run) is False

    run.config = {
        "generation_mode": "v2",
        "execution_mode": "full",
        "compare_parent_run_id": str(uuid.uuid4()),
    }
    assert generation_tasks._is_recoverable_full_v2_run(run) is False


def test_stale_v2_recovery_is_registered_with_beat() -> None:
    schedule = celery_app.conf.beat_schedule["recover-stale-generation-v2-runs"]
    assert schedule["task"] == "app.workers.generation_tasks.recover_stale_generation_v2_runs"


@pytest.mark.asyncio
async def test_lost_v2_worker_lease_cancels_in_flight_pipeline() -> None:
    lease_lost = asyncio.Event()
    cancelled = asyncio.Event()

    async def execution():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(
        generation_tasks._await_v2_execution_or_lease_loss(execution(), lease_lost)
    )
    await asyncio.sleep(0)
    lease_lost.set()

    result = await task

    assert result is generation_tasks._V2_LEASE_LOST
    assert cancelled.is_set()
