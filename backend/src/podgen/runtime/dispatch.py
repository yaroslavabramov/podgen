"""Optional caller-owned dispatch boundary, independent of any product workflow."""

from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT = ContextVar("podgen_dispatch_callback", default=None)


class DispatchStopped(Exception):
    """An owner stopped a new action; received work still needs durable capture."""


@contextmanager
def bind(callback):
    token = _CURRENT.set(callback)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def before_external(kind, identity):
    callback = _CURRENT.get()
    if callback is not None:
        callback(kind, identity)
