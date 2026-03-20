from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.api.generations import get_latest_project_generation
from app.models.generation_run import GenerationStatus


class _FakeResult:
    def __init__(self, scalar=None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDB:
    def __init__(self, results: list[object | None]) -> None:
        self._results = list(results)
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        if not self._results:
            raise AssertionError("Unexpected execute call")
        return _FakeResult(self._results.pop(0))


def _fake_run(status: GenerationStatus) -> SimpleNamespace:
    now = datetime.utcnow()
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=uuid.uuid4(),
        status=status,
        config={},
        current_phase="phase5_planning",
        current_step="plan_episode",
        progress_pct=50.0,
        estimated_cost=None,
        actual_cost=None,
        error_message=None,
        started_at=now,
        completed_at=None,
        created_at=now,
    )


@pytest.mark.asyncio
async def test_get_latest_project_generation_prefers_active_run_over_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_run = _fake_run(GenerationStatus.RUNNING)
    db = _FakeDB([active_run])
    user = SimpleNamespace(id=uuid.uuid4())

    async def _noop_verify(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.generations._verify_project_ownership", _noop_verify)

    response = await get_latest_project_generation(active_run.project_id, user, db)

    assert response.id == active_run.id
    assert response.status == GenerationStatus.RUNNING
    assert db.execute_calls == 1


@pytest.mark.asyncio
async def test_get_latest_project_generation_falls_back_to_latest_failed_when_no_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_run = _fake_run(GenerationStatus.FAILED)
    db = _FakeDB([None, failed_run])
    user = SimpleNamespace(id=uuid.uuid4())

    async def _noop_verify(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.generations._verify_project_ownership", _noop_verify)

    response = await get_latest_project_generation(failed_run.project_id, user, db)

    assert response.id == failed_run.id
    assert response.status == GenerationStatus.FAILED
    assert db.execute_calls == 2
