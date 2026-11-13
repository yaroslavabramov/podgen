"""Operation costs, immutable input access and remote I/O phase boundaries."""

from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from podgen.domain.errors import AccessDenied
from podgen.persistence.database import query
from podgen.persistence.read_authorization import _read_connection, object_io, read_operation
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.model_turns import TurnJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.source_tool_recovery import TurnEvidence
from tests.authorization_helpers import withdraw_blob
from tests.test_read_authorization import diamond


class RemoteStore:
    def __init__(self, repo, on_read=lambda: None):
        self.repo, self.store, self.on_read = repo, repo.objects, on_read

    def __getattr__(self, name):
        return getattr(self.store, name)

    def read(self, project, blob):
        assert self.repo._read_authorization.get().transaction is None
        data = self.store.read(project, blob)
        callback, self.on_read = self.on_read, lambda: None
        callback()
        return data

    def iter_read(self, project, blob):
        assert self.repo._read_authorization.get().transaction is None
        yield from self.store.iter_read(project, blob)
        callback, self.on_read = self.on_read, lambda: None
        callback()


def test_remote_read_has_no_sql_lease_and_reauthorizes_before_returning_bytes(repo, scope):
    project, _ = scope
    root, leaf, blob, _ = diamond(repo, project, depth=2)
    repo.objects = RemoteStore(repo, lambda: withdraw_blob(repo, project, blob))
    with pytest.raises(AccessDenied), read_operation(repo) as operation:
        assert operation is None  # Public operation cannot leak a stale transaction handle.
        repo.get_runtime(project, root)
        repo.read_runtime_blob(project, leaf, blob)
    assert repo._read_authorization.get() is None


def test_remote_io_cannot_invalidate_a_borrowed_sql_connection(repo, scope):
    repo.objects = RemoteStore(repo)
    with read_operation(repo):
        with _read_connection(repo):
            with pytest.raises(RuntimeError, match="object_io_inside_metadata_phase"), object_io(repo):
                pytest.fail("remote request must not start")
    assert repo._read_authorization.get() is None


def test_turn_selection_reads_only_target_bodies_and_enforces_projection_binding(repo, scope):
    project, _ = scope
    provenance, _, _, _ = diamond(repo, project, depth=2)
    journal = EvaluationJournal(repo, project)
    profile = create_profile(repo, project, model="gpt-6.1-sol", context_limit=100000).ref
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="turn-selection",
        author_ref=provenance,
        writer_profile_ref=profile,
        reviewer_profile_ref=profile,
    )
    journal.create_run(command, dict(command=command_payload(command)))
    root = journal.prepare(command.run_id, "draft", {}, dict(provenance=provenance.model_dump(mode="json")))
    target, selected_refs, other_refs = None, [], []
    for index in range(64):
        turn = TurnJournal(journal, root, index)
        row = turn.prepare(dict(input=[dict(text="frozen body")]))
        refs = [
            turn.event(row, "probe", data=dict(full="Semantic payload " * 100, index=index)) for _ in range(3)
        ]
        if index == 63:
            target, selected_refs = row, refs
        else:
            other_refs.extend(refs)
    fetched = []

    def capture(conn, cursor, statement, parameters, context, many):
        if "SELECT payload FROM runtime_records WHERE" in statement:
            fetched.append(parameters)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        evidence = TurnEvidence(journal, root, target)
        assert len(evidence.events(evidence.turn)) == 3
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert len(fetched) == 3
    assert not any(str(ref.record_id) in repr(fetched) for ref in other_refs)
    malformed = journal.read(selected_refs[0])
    malformed["data"]["source_turn_index"] = 62
    malformed_ref = journal.record(malformed)
    with (
        pytest.raises(IntegrityError, match="evaluation_event_turn_mismatch"),
        repo.database.transaction() as tx,
    ):
        query(
            tx,
            """INSERT INTO evaluation_invocation_events
            (project_id,invocation_id,event_id,record_id,event_kind,source_turn_index)
            VALUES(:p,:i,:e,:r,'probe',63)""",
            p=project,
            i=root["invocation_id"],
            e=uuid4(),
            r=malformed_ref.record_id,
        )
    # Historical writers omit the new projection; the database derives it from
    # the same immutable payload, so their events remain selectable after upgrade.
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_invocation_events
            (project_id,invocation_id,event_id,record_id,event_kind)
            VALUES(:p,:i,:e,:r,'probe')""",
            p=project,
            i=root["invocation_id"],
            e=uuid4(),
            r=malformed_ref.record_id,
        )
    assert len(journal.events_for_turn(root, 62)) == 4
    view = TurnEvidence(journal, root, target)
    assert len(view.events(view.turn)) == 3
