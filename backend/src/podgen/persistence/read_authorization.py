"""Successful-node memoization for one read-only, repeatable-read authorization snapshot."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class ReadAuthorization:
    transaction: object
    actor: object
    purpose: str
    completed: dict = field(default_factory=dict)
    write_projects: set = field(default_factory=set)
    owner: object | None = None


_SNAPSHOTS = ContextVar("podgen_fixed_authorization_snapshots", default=())


@contextmanager
def authorized_read(repo, transaction):
    from .publication_scope import owner_for

    publication = owner_for(transaction)
    if publication is not None:
        publication.sync()
    existing = repo._read_authorization.get()
    if (
        existing is not None
        and existing.transaction is transaction
        and existing.actor == repo.actor_id
        and existing.purpose == repo.purpose
    ):
        # A nested pre-publication check shares this exact local snapshot.
        # Publication clears its facts before classification can change access.
        yield
        return
    state = ReadAuthorization(transaction, repo.actor_id, repo.purpose)
    if publication is not None:
        publication.states.append((state, repo))
    token = repo._read_authorization.set(state)
    active = _SNAPSHOTS.set((*_SNAPSHOTS.get(), state))
    try:
        yield
    finally:
        if publication is not None:
            publication.states[:] = [(s, r) for s, r in publication.states if s is not state]
        _SNAPSHOTS.reset(active)
        repo._read_authorization.reset(token)


def authorization_state(repo, transaction=None):
    """Scope guard, including fast paths which read completed directly."""
    from .publication_scope import owner_for

    state = repo._read_authorization.get()
    tx = transaction if transaction is not None else state.transaction if state is not None else None
    publication = owner_for(tx)
    if publication is not None:
        publication.sync()
    return state


def invalidate_publication_facts(repo, transaction, *, effect=None):
    """Publication changes refs/privacy, never roles in this same locked snapshot."""
    from .publication_scope import changed

    if changed(transaction, effect):
        return
    invalidate_transaction(transaction, current=authorization_state(repo, transaction))


def invalidate_transaction(transaction, *, current=None):
    states = [s for s in _SNAPSHOTS.get() if s.transaction is transaction]
    states.extend(op.state for op in _OPERATIONS.get() if op.state.transaction is transaction)
    if current is not None and current.transaction is transaction and all(s is not current for s in states):
        states.append(current)
    for state in states:
        _invalidate_state(state)


def _invalidate_state(state):
    if state is None:
        return
    previous = state.completed.get(("access_facts",))
    roles = dict(previous.roles) if previous is not None else {}
    state.completed.clear()
    if roles:
        from .access_facts import AccessFacts

        facts = AccessFacts(previous.repo, state.transaction)
        facts.roles.update(roles)
        state.completed[("access_facts",)] = facts


def checked_node(repo, transaction, key, load):
    state = authorization_state(repo, transaction)
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


_OPERATIONS = ContextVar("podgen_read_operations", default=())


class ReadOperation:
    """Own local snapshots, releasing every snapshot before remote object I/O."""

    def __init__(self, repo):
        self.repo = repo
        self.state = ReadAuthorization(None, repo.actor_id, repo.purpose, owner=self)
        self.resources = ExitStack()
        self.borrowers = 0

    def connection(self):
        if self.state.transaction is None:
            self.state.transaction = self.resources.enter_context(self.repo.database.read_transaction())
        return self.state.transaction

    def release(self):
        if self.borrowers:
            raise RuntimeError("object_io_inside_metadata_phase")
        try:
            self.resources.close()
        finally:
            self.state.transaction = None
            self.state.completed.clear()


@contextmanager
def read_operation(repo):
    """A bounded operation exposes repository values, never a SQL connection.

    Its SQL and remote object phases are disjoint. No caller can retain a public
    transaction handle while remote I/O releases the metadata snapshot.
    """
    state = repo._read_authorization.get()
    if state is not None and state.actor == repo.actor_id and state.purpose == repo.purpose:
        yield
        return
    operation = ReadOperation(repo)
    token = repo._read_authorization.set(operation.state)
    active = _OPERATIONS.set((*_OPERATIONS.get(), operation))
    try:
        yield
    finally:
        try:
            operation.release()
        finally:
            _OPERATIONS.reset(active)
            repo._read_authorization.reset(token)


@contextmanager
def _read_connection(repo):
    """Private SQL phase. No storage/network I/O while its connection is borrowed."""
    with read_operation(repo):
        state = repo._read_authorization.get()
        if state.owner is None:
            yield state.transaction
            return
        tx = state.owner.connection()
        state.owner.borrowers += 1
        try:
            yield tx
        finally:
            state.owner.borrowers -= 1


@contextmanager
def object_io(repo):
    """Remote storage cannot keep an authorization transaction open while waiting.

    Local projections may reuse the current snapshot. For remote storage, callers
    reauthorize on return; releasing the snapshot also clears all completed nodes.
    """
    from .objects import FileObjectStore

    if not isinstance(repo.objects, FileObjectStore):
        state = repo._read_authorization.get()
        if _SNAPSHOTS.get() or state is not None and state.owner is None:
            raise RuntimeError("remote_storage_inside_unowned_read_snapshot")
        for operation in _OPERATIONS.get():
            operation.release()
    yield


def require_external_boundary():
    if _OPERATIONS.get() or _SNAPSHOTS.get():
        raise RuntimeError("provider_call_inside_read_operation")
