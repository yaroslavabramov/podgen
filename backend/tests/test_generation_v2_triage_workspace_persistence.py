from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2 import source_triage_workspace_persistence as persistence


class _Result:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def scalars(self) -> list[object]:
        return self._items


class _Db:
    def __init__(self, current: object | None) -> None:
        self.current = current

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, _statement: object) -> _Result:
        return _Result([self.current] if self.current is not None else [])


@pytest.mark.asyncio
async def test_workspace_save_rejects_stale_same_attempt() -> None:
    current = SimpleNamespace(
        payload={"_workspace_revision": 4, "corpus_revision": 1, "attempt": 2}
    )
    workspace = {"_workspace_revision": 3, "corpus_revision": 1, "attempt": 2}

    with pytest.raises(
        persistence.SourceTriageWorkspaceConflict,
        match="expected=3, actual=4",
    ):
        await persistence.save_source_triage_workspace(
            _Db(current),
            generation_run_id=uuid.uuid4(),
            workspace=workspace,
        )


@pytest.mark.asyncio
async def test_workspace_save_advances_revision_and_updates_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = SimpleNamespace(
        payload={"_workspace_revision": 4, "corpus_revision": 1, "attempt": 2}
    )
    workspace = {"_workspace_revision": 4, "corpus_revision": 1, "attempt": 2}
    saved_payload: dict = {}
    artifact = object()

    async def save_artifact(_db: object, **kwargs: object) -> object:
        saved_payload.update(kwargs["payload"])
        return artifact

    monkeypatch.setattr(persistence, "save_artifact", save_artifact)

    result = await persistence.save_source_triage_workspace(
        _Db(current),
        generation_run_id=uuid.uuid4(),
        workspace=workspace,
    )

    assert result is artifact
    assert saved_payload["_workspace_revision"] == 5
    assert workspace["_workspace_revision"] == 5


@pytest.mark.asyncio
async def test_old_scope_cannot_replace_new_current_workspace() -> None:
    current = SimpleNamespace(
        payload={"_workspace_revision": 4, "corpus_revision": 2, "attempt": 1}
    )
    workspace = {"_workspace_revision": 4, "corpus_revision": 1, "attempt": 9}

    with pytest.raises(
        persistence.SourceTriageWorkspaceConflict,
        match="scope_conflict",
    ):
        await persistence.save_source_triage_workspace(
            _Db(current),
            generation_run_id=uuid.uuid4(),
            workspace=workspace,
            allow_scope_transition=True,
        )


@pytest.mark.asyncio
async def test_initializer_can_advance_workspace_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = SimpleNamespace(
        payload={"_workspace_revision": 4, "corpus_revision": 1, "attempt": 2}
    )
    workspace = {"_workspace_revision": 0, "corpus_revision": 2, "attempt": 1}
    saved_payload: dict = {}

    async def save_artifact(_db: object, **kwargs: object) -> object:
        saved_payload.update(kwargs["payload"])
        return object()

    monkeypatch.setattr(persistence, "save_artifact", save_artifact)

    await persistence.save_source_triage_workspace(
        _Db(current),
        generation_run_id=uuid.uuid4(),
        workspace=workspace,
        allow_scope_transition=True,
    )

    assert saved_payload["corpus_revision"] == 2
    assert saved_payload["attempt"] == 1
    assert saved_payload["_workspace_revision"] == 5
