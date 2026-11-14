"""Pause only before a new external action; capture never consults pause state."""

from contextlib import contextmanager

from podgen.runtime.dispatch import DispatchStopped
from podgen.runtime.dispatch import bind as bind_dispatch


class ProductionPaused(DispatchStopped):
    pass


class ProductionSuperseded(DispatchStopped):
    pass


@contextmanager
def bind(journal, epoch):
    with bind_dispatch(lambda kind, identity: journal.check_dispatch(epoch)):
        yield
