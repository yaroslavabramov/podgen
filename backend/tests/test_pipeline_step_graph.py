from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.pipeline.graph import _STEP_HISTORY_LIMIT, execute_pipeline_step


class _FakeSelectResult:
    def __init__(self, rows=None, first_row=None) -> None:
        self._rows = rows or []
        self._first_row = first_row

    def fetchall(self):
        return self._rows

    def first(self):
        return self._first_row


class _FakeDB:
    def __init__(self) -> None:
        self._calls = 0

    async def execute(self, _statement):
        self._calls += 1
        if self._calls == 1:
            return _FakeSelectResult(rows=[])
        if self._calls == 2:
            return _FakeSelectResult(first_row=None)
        raise AssertionError("Unexpected DB execute call")


class _FakeCheckpointer:
    async def aget_tuple(self, _config):
        return {"checkpoint": "exists"}


class _FakeCheckpointerContext:
    async def __aenter__(self):
        return _FakeCheckpointer()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAsyncPostgresSaver:
    @classmethod
    def from_conn_string(cls, _conn_string):
        return _FakeCheckpointerContext()


class _FakeCompiled:
    def __init__(self, latest_state, *, expected_config, expected_interrupt_after, yield_node):
        self._latest_state = latest_state
        self._expected_config = expected_config
        self._expected_interrupt_after = expected_interrupt_after
        self._yield_node = yield_node

    async def aget_state(self, _config):
        return self._latest_state

    async def astream(self, _graph_input, config=None, interrupt_after=None):
        assert config == self._expected_config
        assert interrupt_after == self._expected_interrupt_after
        yield {self._yield_node: {"current_phase": "phase5_planning"}}


@pytest.mark.asyncio
async def test_execute_pipeline_step_rerun_prefers_fresh_history_snapshot_for_next_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=None,
        config={"language": "en"},
        langgraph_thread_id="thread-1",
        current_phase="phase5_planning",
        current_step="plan_acts",
    )
    db = _FakeDB()

    stale_latest = SimpleNamespace(
        next=["plan_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-stale"}},
    )
    before_last = SimpleNamespace(
        next=["plan_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-before-last"}},
    )
    fresh_after = SimpleNamespace(
        next=["integrate_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-fresh"}},
    )
    compiled = _FakeCompiled(
        stale_latest,
        expected_config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-before-last"}},
        expected_interrupt_after=["plan_acts"],
        yield_node="plan_acts",
    )

    history_calls = {"count": 0}

    async def _fake_load_state_history(_compiled, _config, limit):
        history_calls["count"] += 1
        if history_calls["count"] == 1:
            assert limit == _STEP_HISTORY_LIMIT
            return [stale_latest, before_last]
        if history_calls["count"] == 2:
            assert limit == _STEP_HISTORY_LIMIT
            return [fresh_after, stale_latest, before_last]
        raise AssertionError("Unexpected history lookup")

    async def _noop_setup(_checkpointer):
        return None

    monkeypatch.setattr("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", _FakeAsyncPostgresSaver)
    monkeypatch.setattr("app.pipeline.graph._ensure_checkpointer_schema", _noop_setup)
    monkeypatch.setattr("app.pipeline.graph.get_compiled_graph", lambda checkpointer=None: compiled)
    monkeypatch.setattr("app.pipeline.graph._load_state_history", _fake_load_state_history)

    result = await execute_pipeline_step(run, db, action="rerun")

    assert result["executed_node"] == "plan_acts"
    assert result["last_node"] == "plan_acts"
    assert result["next_node"] == "integrate_acts"
    assert result["checkpoint_id"] == "chk-fresh"
    assert result["completed"] is False


@pytest.mark.asyncio
async def test_execute_pipeline_step_next_prefers_fresh_history_snapshot_for_resume_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=None,
        config={"language": "en"},
        langgraph_thread_id="thread-1",
        current_phase="phase5_planning",
        current_step="plan_acts",
    )
    db = _FakeDB()

    stale_latest = SimpleNamespace(
        next=["plan_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-stale"}},
    )
    fresh_latest = SimpleNamespace(
        next=["integrate_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-fresh-before"}},
    )
    after_execute = SimpleNamespace(
        next=["write_screenplay"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-after-execute"}},
    )
    compiled = _FakeCompiled(
        stale_latest,
        expected_config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-fresh-before"}},
        expected_interrupt_after=["integrate_acts"],
        yield_node="integrate_acts",
    )

    history_calls = {"count": 0}

    async def _fake_load_state_history(_compiled, _config, limit):
        history_calls["count"] += 1
        if history_calls["count"] == 1:
            assert limit == 1
            return [fresh_latest]
        if history_calls["count"] == 2:
            assert limit == _STEP_HISTORY_LIMIT
            return [after_execute, fresh_latest]
        raise AssertionError("Unexpected history lookup")

    async def _noop_setup(_checkpointer):
        return None

    monkeypatch.setattr("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", _FakeAsyncPostgresSaver)
    monkeypatch.setattr("app.pipeline.graph._ensure_checkpointer_schema", _noop_setup)
    monkeypatch.setattr("app.pipeline.graph.get_compiled_graph", lambda checkpointer=None: compiled)
    monkeypatch.setattr("app.pipeline.graph._load_state_history", _fake_load_state_history)

    result = await execute_pipeline_step(run, db, action="next")

    assert result["executed_node"] == "integrate_acts"
    assert result["last_node"] == "integrate_acts"
    assert result["next_node"] == "write_screenplay"
    assert result["checkpoint_id"] == "chk-after-execute"
    assert result["completed"] is False


@pytest.mark.asyncio
async def test_execute_pipeline_step_can_rerun_specific_earlier_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=None,
        config={"language": "en"},
        langgraph_thread_id="thread-1",
        current_phase="phase5_planning",
        current_step="plan_acts",
    )
    db = _FakeDB()

    latest_before = SimpleNamespace(
        next=["integrate_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-latest-before"}},
    )
    before_plan_acts = SimpleNamespace(
        next=["plan_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-before-plan-acts"}},
    )
    before_plan_episode = SimpleNamespace(
        next=["plan_episode"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-before-plan-episode"}},
    )
    after_target_rerun = SimpleNamespace(
        next=["plan_acts"],
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-after-target"}},
    )
    compiled = _FakeCompiled(
        latest_before,
        expected_config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "chk-before-plan-episode"}},
        expected_interrupt_after=["plan_episode"],
        yield_node="plan_episode",
    )

    history_calls = {"count": 0}

    async def _fake_load_state_history(_compiled, _config, limit):
        assert limit == _STEP_HISTORY_LIMIT
        history_calls["count"] += 1
        if history_calls["count"] == 1:
            return [latest_before, before_plan_acts, before_plan_episode]
        if history_calls["count"] == 2:
            return [after_target_rerun, before_plan_episode]
        raise AssertionError("Unexpected history lookup")

    async def _noop_setup(_checkpointer):
        return None

    monkeypatch.setattr("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", _FakeAsyncPostgresSaver)
    monkeypatch.setattr("app.pipeline.graph._ensure_checkpointer_schema", _noop_setup)
    monkeypatch.setattr("app.pipeline.graph.get_compiled_graph", lambda checkpointer=None: compiled)
    monkeypatch.setattr("app.pipeline.graph._load_state_history", _fake_load_state_history)

    result = await execute_pipeline_step(run, db, action="rerun", target_node="plan_episode")

    assert result["executed_node"] == "plan_episode"
    assert result["last_node"] == "plan_episode"
    assert result["next_node"] == "plan_acts"
    assert result["checkpoint_id"] == "chk-after-target"
    assert result["completed"] is False
    assert "plan_episode" in result["available_rerun_nodes"]
