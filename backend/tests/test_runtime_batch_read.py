"""Public batch reads share authorization while preserving scalar access semantics."""

from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.repository import Repository
from tests.authorization_helpers import withdraw_blob
from tests.test_read_authorization import diamond


def test_batch_reads_requested_bodies_in_order_without_caching_payloads(repo, scope):
    from podgen.persistence.access_facts import access_facts
    from podgen.persistence.read_authorization import read_operation

    project, _ = scope
    root, leaf, _, _ = diamond(repo, project, depth=4)
    expected = [repo.get_runtime(project, ref) for ref in (leaf, root, leaf)]
    fetched = []

    def capture(conn, cursor, sql, parameters, context, many):
        if "SELECT record_id,payload FROM runtime_records" in sql:
            fetched.extend(parameters["ids"])

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        with read_operation(repo):
            assert repo.get_runtime_many(project, [leaf, root, leaf]) == expected
            state = repo._read_authorization.get()
            facts = access_facts(repo, state.transaction)
            assert all("payload" not in row for row in facts.runtimes.rows.values())
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert set(fetched) == {leaf.record_id, root.record_id} and len(fetched) == 2
    assert repo.get_runtime_many(project, []) == []
    with pytest.raises(ContractError, match="runtime_read_batch_too_large"):
        repo.get_runtime_many(project, [leaf] * 65)


def test_batch_fails_atomically_and_rechecks_withdrawal_and_purpose(repo, scope):
    project, _ = scope
    root, leaf, blob, _ = diamond(repo, project, depth=2)
    assert repo.get_runtime_many(project, [root, leaf])
    with pytest.raises(AccessDenied):
        repo.get_runtime_many(project, [root, RuntimeRef(record_kind="user_input", record_id=uuid4())])
    with pytest.raises(AccessDenied):
        repo.get_runtime_many(project, [RuntimeRef(record_kind="tool_execution", record_id=leaf.record_id)])
    secret = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    repo.append_runtime(project, secret, dict(secret="SYNTHETIC evaluator bytes"), audience="evaluator")
    public = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    with pytest.raises(AccessDenied):
        public.get_runtime_many(project, [leaf, secret])
    withdraw_blob(repo, project, blob)
    with pytest.raises(AccessDenied):
        repo.get_runtime_many(project, [root, leaf])
