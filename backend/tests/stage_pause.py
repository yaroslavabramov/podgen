"""Stop a saved role at the production dispatch boundary, before its count is sent."""

from contextlib import contextmanager

import pytest

from podgen.runtime.dispatch import DispatchStopped, bind
from podgen.runtime.evaluation.attempts import attempt_identity


@contextmanager
def pause_before_count(run_id, stage):
    target = str(attempt_identity(run_id, stage, 0, "role", None))

    def stop(kind, identity):
        if kind == "model_count" and identity == target:
            raise DispatchStopped(target)

    with bind(stop), pytest.raises(DispatchStopped) as stopped:
        yield
    assert stopped.value.args == (target,)
