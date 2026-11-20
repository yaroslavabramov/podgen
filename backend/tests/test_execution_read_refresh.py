from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from podgen.persistence.execution.live import StaleInputs
from podgen.persistence.execution.reads import current_inputs


def test_read_refreshes_each_distinct_stale_dependency(monkeypatch):
    pending = {1, 2, 3, 4}
    monkeypatch.setattr(
        "podgen.persistence.execution.inputs.refresh", lambda repo, project, key: pending.remove(key)
    )

    @current_inputs
    def read(repo):
        if pending:
            raise StaleInputs("project", min(pending))
        return "authorized"

    assert read(SimpleNamespace(_read_authorization=ContextVar("read_test", default=None))) == "authorized"


def test_persistently_stale_input_is_bounded(monkeypatch):
    attempted = []
    monkeypatch.setattr(
        "podgen.persistence.execution.inputs.refresh", lambda repo, project, key: attempted.append(key)
    )

    @current_inputs
    def read(repo):
        raise StaleInputs("project", 1)

    with pytest.raises(StaleInputs):
        read(SimpleNamespace(_read_authorization=ContextVar("read_test", default=None)))
    assert attempted == [1, 1]
