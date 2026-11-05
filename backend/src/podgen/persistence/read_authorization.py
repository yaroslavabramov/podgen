"""Successful-node memoization for one read-only, repeatable-read authorization snapshot."""

from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class ReadAuthorization:
    transaction: object
    actor: object
    purpose: str
    completed: dict = field(default_factory=dict)


@contextmanager
def authorized_read(repo, transaction):
    state = ReadAuthorization(transaction, repo.actor_id, repo.purpose)
    token = repo._read_authorization.set(state)
    try:
        yield
    finally:
        repo._read_authorization.reset(token)


def checked_node(repo, transaction, key, load):
    state = repo._read_authorization.get()
    if (
        state is None
        or state.transaction is not transaction
        or state.actor != repo.actor_id
        or state.purpose != repo.purpose
    ):
        return load()
    if key not in state.completed:
        # Only complete authorization enters the memo. The separate active-path guard catches cycles.
        state.completed[key] = load()
    return state.completed[key]
