from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.generations import get_generation_artifacts


class _FakeScalarCollection:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def all(self) -> list[object]:
        return list(self._items)


class _FakeResult:
    def __init__(self, *, scalar=None, scalars: list[object] | None = None) -> None:
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self) -> _FakeScalarCollection:
        return _FakeScalarCollection(self._scalars)


class _FakeDB:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("Unexpected execute call")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_get_generation_artifacts_returns_plan_beat_sheets_and_screenplay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    manifest = SimpleNamespace(
        acts_json=[
            {
                "act_number": 1,
                "story_role": "setup",
                "act_title": "Три банка, один доллар",
                "purpose": "Поставить вопрос",
                "internal_arc": "от аномалии к парадоксу",
                "opening_question": "Почему купюры разные?",
                "ending_turn": "Кто мешает печатать больше?",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "scene_type": "hook",
                        "scene_job": "anomaly",
                        "description": "Три разные купюры в кошельке.",
                        "transition_note": "Но кто это вообще разрешил?",
                    }
                ],
            }
        ]
    )
    segments = [
        SimpleNamespace(
            act_number=1,
            scene_number=1,
            dialogue_json=[
                {"speaker": "A", "text": "Три разные сотни."},
                {"speaker": "B", "text": "Как это возможно?"},
            ],
        )
    ]
    db = _FakeDB(
        [
            _FakeResult(scalar=manifest),
            _FakeResult(scalars=segments),
        ]
    )
    user = SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_user_run(*_args, **_kwargs):
        return run

    monkeypatch.setattr("app.api.generations._get_user_run", _fake_get_user_run)

    response = await get_generation_artifacts(run.id, user, db)

    assert response.run_id == run.id
    assert response.episode_plan[0].act_title == "Три банка, один доллар"
    assert response.episode_plan[0].opening_question == "Почему купюры разные?"
    assert response.beat_sheets[0].scenes[0].scene_job == "anomaly"
    assert response.beat_sheets[0].scenes[0].transition_note == "Но кто это вообще разрешил?"
    assert response.screenplay[0].act_number == 1
    assert response.screenplay[0].scenes[0].scene_number == 1
    assert response.screenplay[0].scenes[0].dialogue[0].text == "Три разные сотни."


@pytest.mark.asyncio
async def test_get_generation_artifacts_handles_missing_manifest_and_filters_bad_dialogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    segments = [
        SimpleNamespace(
            act_number=2,
            scene_number=3,
            dialogue_json=[
                {"speaker": "A", "text": "Живая реплика."},
                {"speaker": "", "text": "bad"},
                {"speaker": "B", "text": ""},
                None,
            ],
        ),
        SimpleNamespace(act_number=2, scene_number=4, dialogue_json=None),
    ]
    db = _FakeDB(
        [
            _FakeResult(scalar=None),
            _FakeResult(scalars=segments),
        ]
    )
    user = SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_user_run(*_args, **_kwargs):
        return run

    monkeypatch.setattr("app.api.generations._get_user_run", _fake_get_user_run)

    response = await get_generation_artifacts(run.id, user, db)

    assert response.episode_plan == []
    assert response.beat_sheets == []
    assert len(response.screenplay) == 1
    assert response.screenplay[0].act_number == 2
    assert len(response.screenplay[0].scenes) == 1
    assert len(response.screenplay[0].scenes[0].dialogue) == 1
    assert response.screenplay[0].scenes[0].dialogue[0].speaker == "A"
    assert response.screenplay[0].scenes[0].dialogue[0].text == "Живая реплика."


@pytest.mark.asyncio
async def test_get_generation_artifacts_hides_stale_later_phase_artifacts_for_step_mode_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        config={
            "execution_mode": "step",
            "step_control": {
                "last_node": "plan_acts",
            },
        },
    )
    manifest = SimpleNamespace(
        acts_json=[
            {
                "act_number": 1,
                "story_role": "setup",
                "act_title": "Акт",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "scene_type": "hook",
                        "scene_job": "anomaly",
                        "description": "Битшит жив.",
                    }
                ],
            }
        ]
    )
    segments = [
        SimpleNamespace(
            act_number=1,
            scene_number=1,
            dialogue_json=[{"speaker": "A", "text": "Старый screenplay."}],
        )
    ]
    db = _FakeDB(
        [
            _FakeResult(scalar=manifest),
            _FakeResult(scalars=segments),
        ]
    )
    user = SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_user_run(*_args, **_kwargs):
        return run

    monkeypatch.setattr("app.api.generations._get_user_run", _fake_get_user_run)

    response = await get_generation_artifacts(run.id, user, db)

    assert len(response.episode_plan) == 1
    assert len(response.beat_sheets) == 1
    assert response.screenplay == []


@pytest.mark.asyncio
async def test_get_generation_artifacts_hides_target_and_later_outputs_while_rerun_is_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        config={
            "execution_mode": "step",
            "step_control": {
                "last_node": "write_screenplay",
                "requested_action": "rerun",
                "requested_target_node": "plan_acts",
            },
        },
    )
    manifest = SimpleNamespace(
        acts_json=[
            {
                "act_number": 1,
                "story_role": "setup",
                "act_title": "Акт",
                "beat_sheet": [
                    {
                        "scene": 1,
                        "scene_type": "hook",
                        "scene_job": "anomaly",
                        "description": "Битшит жив.",
                    }
                ],
            }
        ]
    )
    segments = [
        SimpleNamespace(
            act_number=1,
            scene_number=1,
            dialogue_json=[{"speaker": "A", "text": "Старый screenplay."}],
        )
    ]
    db = _FakeDB(
        [
            _FakeResult(scalar=manifest),
            _FakeResult(scalars=segments),
        ]
    )
    user = SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_user_run(*_args, **_kwargs):
        return run

    monkeypatch.setattr("app.api.generations._get_user_run", _fake_get_user_run)

    response = await get_generation_artifacts(run.id, user, db)

    assert len(response.episode_plan) == 1
    assert response.beat_sheets == []
    assert response.screenplay == []
