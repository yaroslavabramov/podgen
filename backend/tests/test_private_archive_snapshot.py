"""Reuse a bounded local read batch; remote I/O and new operations revoke its grants."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import query
from podgen.runtime.evaluation.model_turns import TurnJournal
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from tests.test_model_step_publication import run, setup
from tests.test_runtime_operation_boundaries import RemoteStore


def _revoke(repo, project):
    with repo.database.transaction() as tx:
        query(
            tx,
            "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
            p=project,
            a=repo.actor_id,
        )


@pytest.mark.parametrize("padding", [0, 70000])
def test_private_batch_bodies_share_only_their_local_authorization_snapshot(repo, scope, padding):
    journal, root, *_ = setup(repo, scope)
    refs = [
        journal.event(
            root,
            "snapshot_probe",
            data=dict(full_text="z" * padding),
            reply=WireReply(f'{{"value":{i}}}'.encode()),
        )
        for i in range(16)
    ]
    lookups = []

    def capture(conn, cursor, sql, parameters, context, many):
        if "FROM execution_entries" in sql and "archive_ids &&" in sql:
            lookups.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        with journal.read_operation():
            records = journal.read_many(refs)
            for i, (ref, record) in enumerate(zip(refs, records, strict=True)):
                assert len(record["data"]["full_text"]) == padding
                blob = BlobRef.model_validate(record["response_blob"])
                assert journal.read_blob(ref, blob) == f'{{"value":{i}}}'.encode()
            journal.authorize_many(refs)
            assert len(lookups) == 1
            with pytest.raises(AccessDenied):
                journal.read_blob(refs[0], BlobRef.model_validate(records[1]["response_blob"]))
            records[0]["event_kind"] = "caller mutation"
            assert journal.read(refs[0])["event_kind"] == "snapshot_probe"
        _revoke(repo, scope[0])
        with pytest.raises(AccessDenied):
            journal.read(refs[0])
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)


def test_remote_blob_read_discards_the_warmed_private_snapshot(repo, scope):
    journal, root, *_ = setup(repo, scope)
    ref = journal.event(root, "snapshot_probe", reply=WireReply(b'{"value":"private"}'))
    repo.objects = RemoteStore(repo, lambda: _revoke(repo, scope[0]))
    with journal.read_operation():
        record = journal.read(ref)
        with pytest.raises(AccessDenied):
            journal.read_blob(ref, BlobRef.model_validate(record["response_blob"]))
    assert repo._read_authorization.get() is None


def test_physical_requests_share_a_large_inherited_context_once_per_batch(repo, scope):
    journal, root, _, _, _, snapshot, manifest, profile, limits, _ = setup(
        repo, scope, context=dict(full_context="z" * 70000)
    )

    def submit(body, **options):
        return WireReply(
            json.dumps(
                dict(
                    id="resp_batch",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        )

    provider = SimpleNamespace(count=lambda _: WireReply(b'{"input_tokens":100}'), submit=submit)
    refs = []
    for index in range(8):
        turn_journal = TurnJournal(journal, root, index)
        turn, phase, request = turn_journal.stage_prepare(snapshot[0])
        run(
            ModelGateway(turn_journal, lambda _: provider),
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=request,
            manifest_snapshot=manifest,
        )
        refs.append(RuntimeRef(record_kind="tool_execution", record_id=turn["request_record_id"]))
    lookups = []

    def capture(conn, cursor, sql, parameters, context, many):
        if "FROM execution_entries" in sql and "archive_ids &&" in sql:
            lookups.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        with journal.read_operation():
            records = journal.read_many(refs)
            for ref, record in zip(refs, records, strict=True):
                assert record["context"]["full_context"] == "z" * 70000
                assert (
                    json.loads(journal.read_blob(ref, BlobRef.model_validate(record["request_blob"])))
                    == snapshot[0]
                )
        assert len(lookups) == 2  # One physical batch and its common immutable context.
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
