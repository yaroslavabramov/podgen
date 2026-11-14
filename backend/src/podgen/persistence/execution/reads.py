"""Refresh disposable input metadata outside a failed owned read snapshot."""

from functools import wraps

from .live import StaleInputs


def current_inputs(method):
    @wraps(method)
    def read(repo, *args, **kwargs):
        for attempt in range(3):
            try:
                return method(repo, *args, **kwargs)
            except StaleInputs as stale:
                state = repo._read_authorization.get()
                if state is not None:
                    if state.owner is None or state.owner.borrowers:
                        raise
                    state.owner.release()
                if attempt == 2:
                    raise
                from .inputs import refresh

                refresh(repo, stale.project, stale.set_id)
        raise AssertionError("unreachable")

    return read


@current_inputs
def authorize_inputs(repo, store, run_id, *, action=False):
    from podgen.persistence.read_authorization import _read_connection

    from .live import authorize

    with _read_connection(repo) as tx:
        return authorize(repo, tx, store.project, store.sets[run_id]["set_id"], action=action)
