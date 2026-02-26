import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.workers import ingestion_tasks as tasks


def _utcnow_naive() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, project):
        self.project = project
        self.commits = 0
        self.rollbacks = 0
        self.executed = []

    async def execute(self, statement):
        self.executed.append(statement)
        return _FakeResult(self.project)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_request_project_content_map_rebuild_enqueues_with_debounce(monkeypatch: pytest.MonkeyPatch):
    project_id = uuid.uuid4()
    project = SimpleNamespace(
        id=project_id,
        content_map_dirty=False,
        content_map_last_requested_at=None,
    )
    db = _FakeDB(project)
    monkeypatch.setattr(tasks.settings, "content_map_rebuild_debounce_seconds", 90)

    captured = {}

    class _AsyncResult:
        id = "task-123"

    def _fake_apply_async(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _AsyncResult()

    monkeypatch.setattr(tasks.rebuild_content_map_for_project, "apply_async", _fake_apply_async)

    task_id = await tasks.request_project_content_map_rebuild(project_id, db, force_now=False)

    assert task_id == "task-123"
    assert project.content_map_dirty is True
    assert isinstance(project.content_map_last_requested_at, datetime)
    assert db.commits == 1
    assert captured["kwargs"]["countdown"] == 90


@pytest.mark.asyncio
async def test_request_project_content_map_rebuild_force_now_uses_zero_countdown(monkeypatch: pytest.MonkeyPatch):
    project_id = uuid.uuid4()
    project = SimpleNamespace(
        id=project_id,
        content_map_dirty=False,
        content_map_last_requested_at=None,
    )
    db = _FakeDB(project)
    monkeypatch.setattr(tasks.settings, "content_map_rebuild_debounce_seconds", 120)

    captured = {}

    class _AsyncResult:
        id = "task-999"

    def _fake_apply_async(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _AsyncResult()

    monkeypatch.setattr(tasks.rebuild_content_map_for_project, "apply_async", _fake_apply_async)

    task_id = await tasks.request_project_content_map_rebuild(project_id, db, force_now=True)

    assert task_id == "task-999"
    assert captured["kwargs"]["countdown"] == 0
    assert captured["kwargs"]["kwargs"]["force_now"] is True


@pytest.mark.asyncio
async def test_rebuild_content_map_task_reschedules_when_within_debounce(monkeypatch: pytest.MonkeyPatch):
    project_id = uuid.uuid4()
    latest_requested_at = _utcnow_naive()
    project = SimpleNamespace(
        id=project_id,
        content_map_dirty=True,
        content_map_last_requested_at=latest_requested_at,
        content_map_last_built_at=None,
    )
    db = _FakeDB(project)
    monkeypatch.setattr(tasks.settings, "content_map_rebuild_debounce_seconds", 120)
    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: _SessionContext(db))

    scheduled = {}

    def _fake_apply_async(*args, **kwargs):
        scheduled["kwargs"] = kwargs
        return SimpleNamespace(id="rescheduled")

    monkeypatch.setattr(tasks.rebuild_content_map_for_project, "apply_async", _fake_apply_async)

    called = {"build": 0}

    async def _fake_build_content_map(_project_id: str, _db):
        called["build"] += 1
        return "should-not-run"

    monkeypatch.setattr("app.ingestion.scout.build_content_map", _fake_build_content_map)

    await tasks._rebuild_content_map_for_project_async(
        str(project_id),
        force_now=False,
        requested_at_iso=latest_requested_at.isoformat(),
    )

    assert called["build"] == 0
    assert "countdown" in scheduled["kwargs"]
    assert scheduled["kwargs"]["countdown"] > 0


@pytest.mark.asyncio
async def test_rebuild_content_map_task_builds_when_due(monkeypatch: pytest.MonkeyPatch):
    project_id = uuid.uuid4()
    latest_requested_at = _utcnow_naive() - timedelta(seconds=180)
    project = SimpleNamespace(
        id=project_id,
        content_map_dirty=True,
        content_map_last_requested_at=latest_requested_at,
        content_map_last_built_at=None,
    )
    db = _FakeDB(project)
    monkeypatch.setattr(tasks.settings, "content_map_rebuild_debounce_seconds", 60)
    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: _SessionContext(db))
    monkeypatch.setattr(tasks.rebuild_content_map_for_project, "apply_async", lambda *args, **kwargs: None)

    called = {"build": 0}

    async def _fake_build_content_map(_project_id: str, _db):
        called["build"] += 1
        return "map-1"

    monkeypatch.setattr("app.ingestion.scout.build_content_map", _fake_build_content_map)

    await tasks._rebuild_content_map_for_project_async(
        str(project_id),
        force_now=False,
        requested_at_iso=latest_requested_at.isoformat(),
    )

    assert called["build"] == 1
    assert project.content_map_dirty is False
    assert isinstance(project.content_map_last_built_at, datetime)


@pytest.mark.asyncio
async def test_rebuild_content_map_task_non_retryable_stops_requeue(monkeypatch: pytest.MonkeyPatch):
    project_id = uuid.uuid4()
    latest_requested_at = _utcnow_naive() - timedelta(seconds=180)
    project = SimpleNamespace(
        id=project_id,
        content_map_dirty=True,
        content_map_last_requested_at=latest_requested_at,
        content_map_last_built_at=None,
    )
    db = _FakeDB(project)
    monkeypatch.setattr(tasks.settings, "content_map_rebuild_debounce_seconds", 60)
    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: _SessionContext(db))

    scheduled = {"count": 0}

    def _fake_apply_async(*args, **kwargs):
        scheduled["count"] += 1
        return SimpleNamespace(id="unexpected")

    monkeypatch.setattr(tasks.rebuild_content_map_for_project, "apply_async", _fake_apply_async)

    async def _fake_build_content_map(_project_id: str, _db):
        raise ValueError(f"No chunks found for project {_project_id}")

    monkeypatch.setattr("app.ingestion.scout.build_content_map", _fake_build_content_map)

    await tasks._rebuild_content_map_for_project_async(
        str(project_id),
        force_now=False,
        requested_at_iso=latest_requested_at.isoformat(),
        attempt=0,
    )

    assert scheduled["count"] == 0
    assert project.content_map_dirty is False


@pytest.mark.asyncio
async def test_rebuild_content_map_task_retry_exhausted_stops_requeue(monkeypatch: pytest.MonkeyPatch):
    project_id = uuid.uuid4()
    latest_requested_at = _utcnow_naive() - timedelta(seconds=180)
    project = SimpleNamespace(
        id=project_id,
        content_map_dirty=True,
        content_map_last_requested_at=latest_requested_at,
        content_map_last_built_at=None,
    )
    db = _FakeDB(project)
    monkeypatch.setattr(tasks.settings, "content_map_rebuild_debounce_seconds", 60)
    monkeypatch.setattr("app.workers.worker_db.worker_session_factory", lambda: _SessionContext(db))

    scheduled = {"count": 0}

    def _fake_apply_async(*args, **kwargs):
        scheduled["count"] += 1
        return SimpleNamespace(id="unexpected")

    monkeypatch.setattr(tasks.rebuild_content_map_for_project, "apply_async", _fake_apply_async)

    async def _fake_build_content_map(_project_id: str, _db):
        raise RuntimeError("temporary failure")

    monkeypatch.setattr("app.ingestion.scout.build_content_map", _fake_build_content_map)

    await tasks._rebuild_content_map_for_project_async(
        str(project_id),
        force_now=False,
        requested_at_iso=latest_requested_at.isoformat(),
        attempt=tasks._CONTENT_MAP_REBUILD_MAX_REQUEUE_ATTEMPTS,
    )

    assert scheduled["count"] == 0
    assert project.content_map_dirty is False
