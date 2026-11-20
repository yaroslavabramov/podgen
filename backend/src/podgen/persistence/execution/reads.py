"""Refresh disposable input metadata outside a failed owned read snapshot."""

from functools import wraps

from .live import StaleInputs


def current_inputs(method):
    @wraps(method)
    def read(repo, *args, **kwargs):
        refreshed = {}
        while True:
            try:
                return method(repo, *args, **kwargs)
            except StaleInputs as stale:
                state = repo._read_authorization.get()
                if state is not None:
                    if state.owner is None or state.owner.borrowers:
                        raise
                    state.owner.release()
                key = (stale.project, stale.set_id)
                refreshed[key] = refreshed.get(key, 0) + 1
                if refreshed[key] > 2:
                    raise
                from .inputs import refresh

                refresh(repo, stale.project, stale.set_id)

    return read


@current_inputs
def authorize_inputs(repo, store, run_id, *, action=False):
    from podgen.persistence.read_authorization import _read_connection

    from .live import authorize

    with _read_connection(repo) as tx:
        return authorize(repo, tx, store.project, store.sets[run_id]["set_id"], action=action)
