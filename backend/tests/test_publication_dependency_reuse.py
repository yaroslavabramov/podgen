"""Unchanged dependencies stay read once; access mutations remain authoritative."""

from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.classification import protect_owned
from podgen.persistence.database import query
from podgen.persistence.publication_scope import AccessChange
from podgen.persistence.read_authorization import (
    authorization_state,
    authorized_read,
    invalidate_publication_facts,
)
from podgen.persistence.repository import Repository
from podgen.persistence.runtime_graph import runtime_graph
from podgen.persistence.runtime_records import append_runtime


def ref():
    return RuntimeRef(record_kind="tool_execution", record_id=uuid4())


def test_1_20_100_batch_records_authorize_each_external_closure_once(repo, scope, producer, record_property):
    from podgen.persistence.artifact_batch import publish
    from podgen.persistence.publication_plan import PublicationBatch, RuntimeNode

    project = scope[0]
    policy = repo.create(
        project, "Policy", dict(policy_kind="data", rules={}), producer=producer, operation_id=uuid4()
    )
    root = ref()
    repo.append_runtime(project, root, dict(policy=policy.ref.model_dump(mode="json")))
    counts, measurements = Counter(), []

    def count(conn, cursor, statement, parameters, context, many):
        for kind, fragment in (
            ("artifact_frontier", "AS access_graph"),
            ("runtime_frontier", "WITH RECURSIVE runtime_reach"),
            ("artifact_body", "SELECT * FROM artifact_revisions"),
            ("inserts", "INSERT INTO runtime_records"),
        ):
            if fragment in statement:
                counts[kind] += 1

    event.listen(repo.database.engine, "before_cursor_execute", count)
    try:
        for number in (1, 20, 100):
            counts.clear()
            nodes = tuple(
                RuntimeNode(
                    str(i),
                    ref(),
                    dict(parent=root.model_dump(mode="json"), policy=policy.ref.model_dump(mode="json")),
                )
                for i in range(number)
            )
            archive_write(
                repo,
                project,
                lambda tx, nodes=nodes: publish(repo, tx, project, PublicationBatch(runtimes=nodes)),
            )
            measurements.append(dict(records=number, **counts))
            assert (
                counts["artifact_frontier"] == counts["runtime_frontier"] == 2
            )  # Evaluation and generation views.
            assert counts["artifact_body"] == 0 and counts["inserts"] == (number + 63) // 64
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", count)
    record_property("batch_dependency_reads", measurements)


def test_unknown_mutation_discards_cached_runtime_topology(repo, scope):
    project, root, secret = scope[0], ref(), ref()
    repo.append_runtime(project, root, dict(text="public"))
    repo.append_runtime(project, secret, dict(text="private"), audience="evaluator")
    public = Repository(repo.database, repo.objects, repo.actor_id)

    def write(tx):
        with authorized_read(public, tx):
            public._runtime(tx, project, root)
            query(
                tx,
                "INSERT INTO runtime_runtime_refs VALUES(:p,:r,:parent,'tool_execution')",
                p=project,
                r=root.record_id,
                parent=secret.record_id,
            )
            invalidate_publication_facts(repo, tx)
            with pytest.raises(AccessDenied):
                public._runtime(tx, project, root)

    archive_write(repo, project, write)


def test_targeted_topology_change_evicts_previously_committed_runtime_graph(repo, scope):
    project, root, secret = scope[0], ref(), ref()
    repo.append_runtime(project, root, dict(text="public"))
    repo.append_runtime(project, secret, dict(text="private"), audience="evaluator")
    public = Repository(repo.database, repo.objects, repo.actor_id)
    with runtime_graph(public):
        public.get_runtime(project, root)  # readonly metadata published before the write

        def write(tx):
            query(
                tx,
                "INSERT INTO runtime_runtime_refs VALUES(:p,:r,:parent,'tool_execution')",
                p=project,
                r=root.record_id,
                parent=secret.record_id,
            )
            invalidate_publication_facts(
                public, tx, effect=AccessChange(runtimes=((project, root.record_id),))
            )
            with pytest.raises(AccessDenied):
                public._runtime(tx, project, root)

        archive_write(public, project, write)


def test_direct_protect_owned_changes_access_but_empty_known_are_noops(repo, scope):
    from io import BytesIO

    project, root = scope[0], ref()
    blob = repo.save_blob(project, BytesIO(b"new owned protection"), "text/plain")
    repo.append_runtime(project, root, dict(blob=blob.model_dump(mode="json")))
    public = Repository(repo.database, repo.objects, repo.actor_id)

    def write(tx):
        with authorized_read(public, tx):
            public._runtime(tx, project, root)
            protect_owned(tx, project, {blob.sha256: root.record_id})
            with pytest.raises(AccessDenied):
                public._runtime(tx, project, root)
            repo._runtime(tx, project, root)
            key = "runtime", project, root.record_kind, root.record_id
            for hashes in ({}, {blob.sha256: root.record_id}):
                protect_owned(tx, project, hashes)
                assert key in authorization_state(repo, tx).completed

    archive_write(repo, project, write)


@pytest.mark.parametrize("ending", ["rollback", "commit"])
def test_archive_callback_cannot_end_its_root_transaction(repo, scope, ending):
    def write(tx):
        getattr(tx, ending)()
        return "must not succeed"

    with pytest.raises(RuntimeError, match="publication_root_transaction_changed"):
        archive_write(repo, scope[0], write)


def test_root_replacement_failure_remains_sticky(repo, scope):
    from podgen.persistence.publication_scope import owner_for

    def write(tx):
        owner = owner_for(tx)
        tx.rollback()
        for _ in range(2):
            with pytest.raises(RuntimeError, match="publication_root_transaction_changed"):
                owner.sync()

    with pytest.raises(RuntimeError, match="publication_root_transaction_changed"):
        archive_write(repo, scope[0], write)


@pytest.mark.parametrize("first", ["artifact_load", "artifact_expand", "runtime", "blob"])
def test_held_fact_objects_recheck_local_hits_after_savepoint_rollback(repo, scope, producer, first):
    from io import BytesIO

    from podgen.persistence.access_facts import access_facts

    project, root = scope[0], ref()
    blob = repo.save_blob(project, BytesIO(b"held facts rollback"), "text/plain")

    def write(tx):
        facts = access_facts(repo, tx)
        artifacts, runtimes, blobs = facts.artifacts, facts.runtimes, facts.blobs
        nested = tx.begin_nested()
        policy = repo._create(
            tx,
            project,
            "Policy",
            dict(policy_kind="data", rules={}),
            producer=producer,
            operation_id=uuid4(),
        )
        append_runtime(repo, tx, project, root, dict(text="rolled back"))
        artifacts.expand([policy.ref.revision_id])
        runtimes.row(project, root)
        protect_owned(tx, project, {blob.sha256: root.record_id})
        artifacts.expand([policy.ref.revision_id])
        runtimes.row(project, root)
        blobs.load(project, [blob.storage_key])
        assert blobs.rows[project, blob.storage_key]["evaluator_only"]
        nested.rollback()
        if first == "artifact_load":
            artifacts.load([policy.ref.revision_id])
        elif first == "artifact_expand":
            artifacts.expand([policy.ref.revision_id])
        elif first == "runtime":
            assert runtimes.row(project, root) is None
        else:
            blobs.load(project, [blob.storage_key])
            assert not blobs.rows[project, blob.storage_key]["evaluator_only"]
        assert facts.artifacts is artifacts and facts.runtimes is runtimes and facts.blobs is blobs
        assert artifacts.row(policy.ref) is None
        assert runtimes.row(project, root) is None
        blobs.load(project, [blob.storage_key])
        assert not blobs.rows[project, blob.storage_key]["evaluator_only"]

    archive_write(repo, project, write)
