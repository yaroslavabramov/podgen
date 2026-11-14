"""Full artifact publication: bounded reads, field contracts and storage boundaries."""

from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.database import query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.read_authorization import read_operation
from podgen.persistence.repository import Repository
from tests.factories import Factory


@pytest.fixture
def factory(repo, scope, producer):
    return Factory(repo, *scope, producer).setup()


def transaction_guard(repo, monkeypatch, finalize):
    """AUTOCOMMIT run locks are allowed; storage cannot hold a SQL transaction."""
    active = set()

    def begin(connection):
        if connection.get_execution_options().get("isolation_level") != "AUTOCOMMIT":
            active.add(connection)

    def end(connection):
        active.discard(connection)

    for name, listener in (("begin", begin), ("commit", end), ("rollback", end)):
        event.listen(repo.database.engine, name, listener)
        finalize(lambda name=name, listener=listener: event.remove(repo.database.engine, name, listener))
    for name in ("read", "iter_read", "publish", "verify_existing"):
        original = getattr(repo.objects, name)

        def checked(*args, _original=original, **kwargs):
            assert not active, "object I/O inside artifact SQL transaction"
            return _original(*args, **kwargs)

        def stream(*args, _original=original, **kwargs):
            assert not active, "object I/O inside artifact SQL transaction"
            for chunk in _original(*args, **kwargs):
                assert not active, "object I/O inside artifact SQL transaction"
                yield chunk

        monkeypatch.setattr(repo.objects, name, stream if name == "iter_read" else checked)


def test_transaction_guard_detects_archive_callback_io(factory, monkeypatch, request):
    from podgen.persistence.archive_authorization import archive_write

    f = factory
    _, blob = f.source()
    transaction_guard(f.repo, monkeypatch, request.addfinalizer)
    with pytest.raises(AssertionError, match="object I/O inside"):
        archive_write(f.repo, f.project, lambda _: f.repo.objects.read(f.project, blob))


def test_each_reference_field_is_validated_after_unique_read(factory):
    f = factory
    source, _ = f.source()
    data = f.policies["data"]
    for occurrence in (
        dict(data_policy_ref=data, source_policy_ref=data),
        dict(source_policy_ref=data, data_policy_ref=data),
    ):
        with pytest.raises(ContractError, match="policy_subtype_mismatch"):
            f.make("SourceRevision", source.payload | dict(source_metadata_evidence=[occurrence]))


def test_one_reference_keeps_both_lineage_and_ordinary_dependency(factory):
    f = factory
    old = f.note()
    body = old.payload | dict(supersedes_note_refs=[old.ref.model_dump(mode="json")])
    new = f.make("ResearchNote", body, input_refs=[old.ref, old.ref])
    with f.repo.database.read_transaction() as tx:
        relations = set(
            query(
                tx,
                "SELECT relation FROM artifact_dependencies WHERE project_id=:p AND consumer_id=:c AND input_id=:i",
                p=f.project,
                c=new.ref.revision_id,
                i=old.ref.revision_id,
            ).scalars()
        )
        assert relations == {"lineage", "uses_content"}
    assert new.input_refs == [old.ref]


def test_idempotent_replay_does_not_rewrite_legacy_edge_roles(factory):
    f = factory
    parent = f.note()
    body = parent.payload | dict(supersedes_note_refs=[parent.ref.model_dump(mode="json")])
    operation = uuid4()
    kwargs = dict(producer=f.producer, operation_id=operation, episode_id=f.episode)
    legacy = f.repo.create(f.project, "ResearchNote", body, **kwargs)
    replay = f.repo.create(f.project, "ResearchNote", body, input_refs=[parent.ref], **kwargs)
    assert replay == legacy
    with f.repo.database.read_transaction() as tx:
        assert set(
            query(
                tx,
                "SELECT relation FROM artifact_dependencies WHERE project_id=:p AND consumer_id=:c",
                p=f.project,
                c=legacy.ref.revision_id,
            ).scalars()
        ) == {"lineage"}


def test_duplicate_reference_occurrences_do_not_repeat_database_work(factory, record_property):
    f = factory
    source, _ = f.source()
    policy = f.policies["data"]
    counts = Counter()

    def count(conn, cursor, statement, parameters, context, many):
        counts["sql"] += 1
        if "SELECT * FROM artifact_revisions WHERE revision_id=ANY" in statement:
            counts["body_batches"] += 1
        if "SELECT * FROM artifact_revisions WHERE revision_id=" in statement and "=ANY" not in statement:
            counts["scalar_bodies"] += 1

    metrics = []
    event.listen(f.repo.database.engine, "before_cursor_execute", count)
    try:
        for size in (1, 20, 100):
            counts.clear()
            body = source.payload | dict(
                source_id=str(uuid4()), source_metadata_evidence=[dict(basis_ref=policy)] * size
            )
            f.make("SourceRevision", body)
            metrics.append(dict(occurrences=size, **counts))
    finally:
        event.remove(f.repo.database.engine, "before_cursor_execute", count)
    record_property("duplicate_reference_publication", metrics)
    assert len({m["sql"] for m in metrics}) == 1
    assert all(m["body_batches"] == 1 and m.get("scalar_bodies", 0) == 0 for m in metrics)


def test_blob_verification_is_outside_transaction_and_acl_is_fresh(factory, monkeypatch, request):
    f = factory
    source, _ = f.source()
    transaction_guard(f.repo, monkeypatch, request.addfinalizer)
    original = f.repo.objects.iter_read
    withdrawn = False

    def withdraw_after_read(*args, **kwargs):
        nonlocal withdrawn
        yield from original(*args, **kwargs)
        with f.repo.database.transaction() as tx:
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                p=f.project,
                a=f.repo.actor_id,
            )
        withdrawn = True

    monkeypatch.setattr(f.repo.objects, "iter_read", withdraw_after_read)
    with pytest.raises(AccessDenied):
        f.make("SourceRevision", source.payload | dict(source_id=str(uuid4())))
    assert withdrawn


def test_byte_withdrawal_after_verification_cannot_publish(factory, monkeypatch):
    f = factory
    source, _ = f.source()
    original = f.repo.objects.iter_read

    def withdraw(*args, **kwargs):
        yield from original(*args, **kwargs)
        DependencyLedger(f.repo).invalidate(
            f.project, source.ref, reason="Withdrawal during verification", category="access"
        )

    monkeypatch.setattr(f.repo.objects, "iter_read", withdraw)
    with pytest.raises(AccessDenied):
        f.make("SourceRevision", source.payload | dict(source_id=str(uuid4())))


def test_evaluator_classification_after_verification_cannot_publish_public_alias(factory, monkeypatch):
    f = factory
    source, blob = f.source()
    original = f.repo.objects.iter_read
    classified = False

    def classify(*args, **kwargs):
        nonlocal classified
        yield from original(*args, **kwargs)
        if not classified:
            classified = True
            f.make(
                "Policy",
                dict(policy_kind="data", rules=dict(owned_blob=blob.model_dump(mode="json"))),
                audience="evaluator",
            )

    monkeypatch.setattr(f.repo.objects, "iter_read", classify)
    with pytest.raises(AccessDenied):
        f.make("SourceRevision", source.payload | dict(source_id=str(uuid4())))
    assert classified


def test_remote_verification_releases_outer_snapshot_and_rechecks_withdrawal(factory):
    f = factory
    source, _ = f.source()

    class RemoteStore:
        def iter_read(self, project, ref):
            state = remote._read_authorization.get()
            assert state is not None and state.transaction is None
            DependencyLedger(f.repo).invalidate(
                f.project, source.ref, reason="Remote read withdrawal", category="access"
            )
            yield from f.repo.objects.iter_read(project, ref)

    remote = Repository(f.repo.database, RemoteStore(), f.repo.actor_id, purpose=f.repo.purpose)
    with read_operation(remote):
        remote.get(f.project, source.ref)
        assert remote._read_authorization.get().transaction is not None
        with pytest.raises(AccessDenied):
            remote.create(
                f.project,
                "SourceRevision",
                source.payload | dict(source_id=str(uuid4())),
                producer=f.producer,
                operation_id=uuid4(),
            )
