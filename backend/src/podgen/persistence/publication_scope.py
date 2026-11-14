"""SQL publication scope guards and invalidation; no reusable descriptor cache."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps


@dataclass(frozen=True)
class Append:
    artifacts: tuple = ()
    runtimes: tuple = ()


@dataclass(frozen=True)
class AccessChange:
    runtimes: tuple = ()


_OWNERS = ContextVar("podgen_publication_scopes", default=())


def owner_for(tx):
    return next((owner for owner in reversed(_OWNERS.get()) if owner.tx is tx), None)


def changed(tx, effect=None):
    owner = owner_for(tx)
    if owner is None:
        return False
    owner.change(effect)
    return True


def notify(tx, effect):
    if not changed(tx, effect):
        from .read_authorization import invalidate_transaction

        invalidate_transaction(tx)


def access_mutation(method):
    @wraps(method)
    def mutate(tx, *args, **kwargs):
        try:
            return method(tx, *args, **kwargs)
        finally:
            notify(tx, AccessChange())

    return mutate


@contextmanager
def publication_scope(tx, project):
    owner = Scope(tx, project)
    token = _OWNERS.set((*_OWNERS.get(), owner))
    try:
        yield owner
    finally:
        _OWNERS.reset(token)


class Scope:
    def __init__(self, tx, project):
        self.tx, self.project, self.states = tx, project, []
        self.scope = self.identity()
        self.root = self.scope[0]

    def identity(self):
        return self.tx.get_transaction(), self.tx.get_nested_transaction()

    def sync(self):
        current = self.identity()
        if current[0] is not self.root:
            self.project = None
            self.evict_runtime_graphs()
            self.reset_states(scope_changed=True)
            raise RuntimeError("publication_root_transaction_changed")
        if current != self.scope:
            self.scope = current
            self.evict_runtime_graphs()
            self.reset_states(scope_changed=True)

    def reset_states(self, *, scope_changed=False, preserve_identity=False):
        for state, _repo in self.states:
            if scope_changed:
                state.write_projects.intersection_update(
                    {self.project} if self.project is not None else set()
                )
            facts = state.completed.get(("access_facts",))
            identities = {
                key: value
                for key, value in state.completed.items()
                if preserve_identity
                and isinstance(key, tuple)
                and key
                and key[0] in ("private_run_owner", "private_input_owner")
            }
            if facts is not None:
                facts.reset(preserve_roles=preserve_identity)
            state.completed.clear()
            state.completed.update(identities)
            if facts is not None:
                state.completed[("access_facts",)] = facts

    def change(self, effect):
        self.sync()
        if isinstance(effect, Append):
            # Appending complete immutable nodes does not change existing access.
            # A formerly missing identity invalidates its derived denial/closure.
            missing = any(
                bool(set(effect.artifacts) & facts.artifacts.missing)
                or any(key in facts.runtimes.missing for key in effect.runtimes)
                for state, _ in self.states
                if (facts := state.completed.get(("access_facts",))) is not None
            )
            if missing:
                self.reset_states(preserve_identity=True)
            return
        if isinstance(effect, AccessChange):
            self.evict_runtime_graphs(effect.runtimes)
        elif not isinstance(effect, Append):
            self.evict_runtime_graphs()
        self.reset_states(preserve_identity=isinstance(effect, (Append, AccessChange)))

    def evict_runtime_graphs(self, identities=None):
        for graph in {repo._runtime_graph.get() for _, repo in self.states} - {None}:
            if identities is None:
                graph.clear()
            else:
                for project, identity in identities:
                    graph.discard(project, identity)
