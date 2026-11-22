"""One bounded instruction assembly observes one immutable set of local Markdown bytes."""

from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT = ContextVar("writing_instruction_snapshot", default=None)


def current():
    return _CURRENT.get()


@contextmanager
def instruction_snapshot():
    if current() is not None:
        yield
        return
    token = _CURRENT.set({})
    try:
        yield
    finally:
        _CURRENT.reset(token)
