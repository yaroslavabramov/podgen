"""Failed snapshot cleanup must not leak operation ownership or cached facts."""

from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from podgen.persistence.read_authorization import (
    _read_connection,
    read_operation,
    require_external_boundary,
)


def test_failed_snapshot_close_clears_facts_and_restores_context():
    @contextmanager
    def transaction():
        yield object()
        raise RuntimeError("snapshot close failed")

    repo = SimpleNamespace(
        actor_id="actor",
        purpose="test",
        database=SimpleNamespace(read_transaction=transaction),
        _read_authorization=ContextVar("test_failed_snapshot_close", default=None),
    )
    with pytest.raises(RuntimeError, match="snapshot close failed"):
        with read_operation(repo), _read_connection(repo):
            state = repo._read_authorization.get()
            state.completed["proof"] = object()
    assert state.transaction is None
    assert state.completed == {}
    assert repo._read_authorization.get() is None
    require_external_boundary()
    with read_operation(repo):
        assert repo._read_authorization.get() is not state
