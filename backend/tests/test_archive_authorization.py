"""Evaluation publication uses one authorization DAG check and serializes with source withdrawal."""

import io
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event

from podgen.domain.errors import AccessDenied
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.authorization_helpers import withdraw_blob
from tests.test_read_authorization import diamond


def test_journal_record_checks_diamond_once_in_its_publication_transaction(repo, scope):
    project, _ = scope
    root, _, _, nodes = diamond(repo, project)
    fetched = []

    def record(conn, cursor, statement, parameters, context, many):
        if "SELECT * FROM runtime_records WHERE" in statement:
            fetched.append(parameters)

    event.listen(repo.database.engine, "before_cursor_execute", record)
    try:
        ref = EvaluationJournal(repo, project).record(dict(basis=root.model_dump(mode="json")))
        assert len(fetched) <= len(nodes) + 1
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", record)
    assert repo.get_runtime(project, ref)


def test_withdrawal_waits_for_archive_publication_then_closes_new_derived_bytes(repo, scope, monkeypatch):
    from podgen.persistence.scope import ScopeAccess

    project, _ = scope
    root, leaf, source_blob, _ = diamond(repo, project, depth=2)
    new_body = b"SYNTHETIC newly archived derivative"
    new_blob = repo.save_blob(project, io.BytesIO(new_body), "text/plain")
    reached = Event()
    revoking = Event()
    original = ScopeAccess.runtime
    paused = False

    def pause(self, tx, project_id, ref):
        nonlocal paused
        result = original(self, tx, project_id, ref)
        if ref == leaf and not paused:
            paused = True
            reached.set()
            assert revoking.wait(10)
        return result

    monkeypatch.setattr(ScopeAccess, "runtime", pause)

    def revoke():
        assert reached.wait(10)
        revoking.set()
        withdraw_blob(repo, project, source_blob)

    journal = EvaluationJournal(repo, project)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(revoke)
        saved = journal.record(
            dict(basis=root.model_dump(mode="json"), blob=new_blob.model_dump(mode="json"))
        )
        future.result(timeout=15)
    with pytest.raises(AccessDenied):
        journal.read(saved)
    with pytest.raises(AccessDenied):
        repo.save_blob(project, io.BytesIO(new_body), "text/plain")
    with pytest.raises(AccessDenied):
        journal.record(dict(basis=root.model_dump(mode="json")))
