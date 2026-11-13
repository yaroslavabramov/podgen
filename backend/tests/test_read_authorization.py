"""Memoize only a complete authorization in one read snapshot, never across public calls."""

import io
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.access_facts import access_facts
from podgen.persistence.database import json_text, query
from podgen.persistence.read_authorization import read_operation
from podgen.persistence.repository import Repository


def diamond(repo, project, depth=6):
    blob = repo.save_blob(project, io.BytesIO(b"SYNTHETIC private authorization graph"), "text/plain")
    leaf = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, leaf, dict(blob=blob.model_dump(mode="json")))
    previous = [leaf, leaf]
    nodes = [leaf]
    for _ in range(depth):
        current = []
        for _ in range(2):
            ref = RuntimeRef(record_kind="user_input", record_id=uuid4())
            repo.append_runtime(
                project,
                ref,
                dict(left=previous[0].model_dump(mode="json"), right=previous[1].model_dump(mode="json")),
            )
            current.append(ref)
            nodes.append(ref)
        previous = current
    root = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(
        project,
        root,
        dict(left=previous[0].model_dump(mode="json"), right=previous[1].model_dump(mode="json")),
    )
    return root, leaf, blob, nodes + [root]


def test_diamond_visits_each_runtime_once_per_public_read(repo, scope):
    project, _ = scope
    root, _, _, nodes = diamond(repo, project)
    fetches = []

    def record(conn, cursor, statement, parameters, context, many):
        if "WITH RECURSIVE runtime_reach" in statement:
            fetches.append(parameters)

    event.listen(repo.database.engine, "before_cursor_execute", record)
    try:
        for count in (1, 2):
            with read_operation(repo):
                repo.get_runtime(project, root)
                state = repo._read_authorization.get()
                assert len(access_facts(repo, state.transaction).runtimes.rows) == len(nodes)
            assert len(fetches) == count
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", record)


def test_concurrent_withdrawal_uses_one_snapshot_and_next_read_is_fresh(repo, scope, monkeypatch):
    from podgen.persistence.scope import ScopeAccess

    project, _ = scope
    root, leaf, blob, _ = diamond(repo, project, depth=2)
    original = ScopeAccess.runtime
    withdrawn = False

    def revoke_after_leaf(self, tx, project_id, ref):
        nonlocal withdrawn
        result = original(self, tx, project_id, ref)
        if ref == leaf and not withdrawn:
            withdrawn = True
            from tests.authorization_helpers import withdraw_blob

            withdraw_blob(repo, project, blob)
        return result

    monkeypatch.setattr(ScopeAccess, "runtime", revoke_after_leaf)
    assert repo.get_runtime(project, root)
    assert withdrawn
    with pytest.raises(AccessDenied):
        repo.get_runtime(project, root)


def test_completed_memo_does_not_hide_cycles_or_cross_purpose_acl(repo, scope):
    project, _ = scope
    a, b = [RuntimeRef(record_kind="tool_execution", record_id=uuid4()) for _ in range(2)]
    with repo.database.transaction() as tx:
        for ref, child in ((a, b), (b, a)):
            query(
                tx,
                "INSERT INTO runtime_records VALUES(:p,:r,:k,CAST(:body AS json),'project')",
                p=project,
                r=ref.record_id,
                k=ref.record_kind,
                body=json_text(dict(child=child.model_dump(mode="json"))),
            )
    with pytest.raises(AccessDenied):
        repo.get_runtime(project, a)
    secret = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    repo.append_runtime(project, secret, dict(secret="SYNTHETIC evaluator-only"), audience="evaluator")
    assert repo.get_runtime(project, secret)
    public = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    with pytest.raises(AccessDenied):
        public.get_runtime(project, secret)
    with repo.database.transaction() as tx:
        query(
            tx, "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a", p=project, a=repo.actor_id
        )
    with pytest.raises(AccessDenied):
        repo.get_runtime(project, secret)
