"""Locked membership is reused only in its own transaction, never across withdrawals."""

from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.access_facts import access_facts
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.database import query
from podgen.persistence.runtime_publication import append_evaluation_batch
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.source_search_access import IndexAccess


@pytest.mark.parametrize("boundary", ["archive", "index"])
def test_locked_role_is_not_selected_again_in_same_transaction(repo, scope, boundary):
    project, _ = scope
    journal = EvaluationJournal(repo, project)
    ref = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, ref, dict(synthetic=True))
    roles = []

    def capture(conn, cursor, sql, parameters, context, many):
        if sql.startswith("SELECT role FROM project_members"):
            roles.append(sql)

    def write(tx):
        journal.authorize(tx)
        append_evaluation_batch(
            repo,
            tx,
            project,
            [
                (
                    RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
                    dict(parent=ref.model_dump(mode="json")),
                    False,
                )
            ],
        )
        # Both publication invalidations preserve only role facts. Fresh access
        # checks remain necessary for all refs, blobs and classifications.
        journal.authorize(tx)
        facts = access_facts(repo, tx)
        assert facts.roles == {project: dict(role="owner")}
        assert not facts.runtimes.rows and not facts.artifacts.rows and not facts.blobs.rows

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        if boundary == "archive":
            archive_write(repo, project, write)
        else:
            with IndexAccess(journal, ref).transaction() as (tx, _):
                journal.authorize(tx)
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert len(roles) == 1 and "FOR SHARE" in roles[0]
    assert repo._read_authorization.get() is None


@pytest.mark.parametrize("boundary", ["archive", "index"])
@pytest.mark.parametrize("withdrawal", ["missing", "demotion"])
def test_next_transaction_observes_missing_or_demoted_locked_member(repo, scope, boundary, withdrawal):
    project, _ = scope
    journal = EvaluationJournal(repo, project)
    ref = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, ref, dict(synthetic=True))

    def access():
        if boundary == "archive":
            archive_write(repo, project, lambda tx: journal.authorize(tx))
        else:
            with IndexAccess(journal, ref).transaction():
                pass

    access()
    with repo.database.transaction() as tx:
        query(
            tx,
            "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a"
            if withdrawal == "missing"
            else "UPDATE project_members SET role='editor' WHERE project_id=:p AND actor_id=:a",
            p=project,
            a=repo.actor_id,
        )
    with pytest.raises(AccessDenied):
        access()
    assert repo._read_authorization.get() is None
