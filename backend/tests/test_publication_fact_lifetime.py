"""Adversarial SQL publication lifetimes; no provider or media processing is involved."""

from contextlib import ExitStack
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.classification import classify_evaluator
from podgen.persistence.database import json_text, query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.execution.resources import compile_resources
from podgen.persistence.privacy import revoke_content
from podgen.persistence.private_journal.contracts import Checkpoint, SemanticResult
from podgen.persistence.private_journal.semantic import publish as publish_semantic
from podgen.persistence.private_journal.writer import commit
from podgen.persistence.read_authorization import authorized_read
from podgen.persistence.repository import Repository
from podgen.persistence.runtime_publication import append_evaluation_batch
from podgen.persistence.runtime_records import append_runtime
from tests.test_execution_parent_privacy import exchange as exchange
from tests.test_private_journal_privacy import private_scope as private_scope


def reference():
    return RuntimeRef(record_kind="tool_execution", record_id=uuid4())


def generation(repo):
    return Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")


def blob(repo, project, body):
    return repo.save_blob(project, BytesIO(body), "text/plain")


def event(repo, project, producer):
    owner = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(synthetic="event witness")),
        producer=producer,
        operation_id=uuid4(),
    )
    return DependencyLedger(repo).invalidate(
        project, owner.ref, reason="SYNTHETIC event witness", category="quality"
    )


@pytest.mark.parametrize("publication", ["scalar", "batch"])
def test_classification_invalidates_both_active_purpose_views(repo, scope, publication):
    project = scope[0]
    shared = blob(repo, project, b"SYNTHETIC formerly public bytes")
    root = reference()
    repo.append_runtime(project, root, dict(blob=shared.model_dump(mode="json")))
    public = generation(repo)

    def write(tx):
        with authorized_read(public, tx):
            for reader in (repo, public):
                reader._runtime(tx, project, root)
                reader._check_blob(tx, project, shared)
            secret = reference()
            payload = dict(blob=shared.model_dump(mode="json"))
            if publication == "scalar":
                append_runtime(repo, tx, project, secret, payload, audience="evaluator")
            else:
                append_evaluation_batch(repo, tx, project, [(secret, payload, False)])
            repo._runtime(tx, project, root)
            repo._check_blob(tx, project, shared)
            with pytest.raises(AccessDenied):
                public._runtime(tx, project, root)
            with pytest.raises(AccessDenied):
                public._check_blob(tx, project, shared)
            with pytest.raises(AccessDenied):
                repo._check_blob(tx, project, shared, public_only=True)

    archive_write(repo, project, write)


def test_missing_runtime_becomes_readable_in_both_views_after_publication(repo, scope):
    project, missing, public = scope[0], reference(), generation(repo)

    def write(tx):
        with authorized_read(public, tx):
            for reader in (repo, public):
                with pytest.raises(AccessDenied):
                    reader._runtime(tx, project, missing)
            append_runtime(repo, tx, project, missing, dict(text="SYNTHETIC complete publication"))
            for reader in (repo, public):
                reader._runtime(tx, project, missing)

    archive_write(repo, project, write)


def test_missing_artifact_becomes_readable_in_both_views_after_publication(
    repo, scope, producer, monkeypatch
):
    project, public = scope[0], generation(repo)
    missing = ArtifactRef(kind="Policy", artifact_id=uuid4(), revision_id=uuid4())
    identities = iter((missing.revision_id, uuid4()))
    monkeypatch.setattr("podgen.persistence.artifact_writer.uuid4", lambda: next(identities))

    def write(tx):
        with authorized_read(public, tx):
            for reader in (repo, public):
                with pytest.raises(AccessDenied):
                    reader._authorize_artifact(tx, project, missing)
            created = repo._create(
                tx,
                project,
                "Policy",
                dict(policy_kind="data", rules=dict(synthetic="complete artifact publication")),
                producer=producer,
                operation_id=uuid4(),
                artifact_id=missing.artifact_id,
            )
            assert created.ref == missing
            for reader in (repo, public):
                reader._authorize_artifact(tx, project, missing)

    archive_write(repo, project, write)


@pytest.mark.parametrize("depth", [1, 2])
def test_savepoint_rollback_discards_positive_descriptors_and_decisions(repo, scope, depth):
    project, reused, public = scope[0], reference(), generation(repo)
    old = blob(repo, project, b"SYNTHETIC rolled-back topology")
    current = blob(repo, project, b"SYNTHETIC replacement topology")

    def write(tx):
        with authorized_read(public, tx):
            with pytest.raises(RuntimeError, match="rollback publication"):
                with ExitStack() as nested:
                    for _ in range(depth):
                        nested.enter_context(tx.begin_nested())
                    append_runtime(repo, tx, project, reused, dict(blob=old.model_dump(mode="json")))
                    for reader in (repo, public):
                        reader._runtime(tx, project, reused)
                    raise RuntimeError("rollback publication")
            # This must fail at authorization, before any missing payload SELECT.
            for reader in (repo, public):
                with pytest.raises(AccessDenied):
                    reader._runtime(tx, project, reused)
            append_runtime(repo, tx, project, reused, dict(blob=current.model_dump(mode="json")))
            append_runtime(
                repo,
                tx,
                project,
                reference(),
                dict(blob=old.model_dump(mode="json")),
                audience="evaluator",
            )
            # A stale positive descriptor would still bind reused to the protected old SHA.
            for reader in (repo, public):
                reader._runtime(tx, project, reused)
                reader._check_blob(tx, project, current)
            with pytest.raises(AccessDenied):
                public._check_blob(tx, project, old)

    archive_write(repo, project, write)


@pytest.mark.parametrize("mutation", ["classification", "withdrawal"])
def test_savepoint_rollback_restores_access_after_a_denial(repo, scope, producer, mutation):
    project, root, public = scope[0], reference(), generation(repo)
    shared = blob(repo, project, b"SYNTHETIC reversible access facts")
    repo.append_runtime(project, root, dict(blob=shared.model_dump(mode="json")))
    owner = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(blob=shared.model_dump(mode="json"))),
        producer=producer,
        operation_id=uuid4(),
    )
    witness = event(repo, project, producer)

    def write(tx):
        with authorized_read(public, tx):
            for reader in (repo, public):
                reader._runtime(tx, project, root)
            with pytest.raises(RuntimeError, match="rollback access"):
                with tx.begin_nested():
                    if mutation == "classification":
                        append_runtime(
                            repo,
                            tx,
                            project,
                            reference(),
                            dict(blob=shared.model_dump(mode="json")),
                            audience="evaluator",
                        )
                    else:
                        revoke_content(tx, project, owner.ref.revision_id, witness, "SYNTHETIC withdrawal")
                        with pytest.raises(AccessDenied):
                            repo._runtime(tx, project, root)
                    with pytest.raises(AccessDenied):
                        public._runtime(tx, project, root)
                    raise RuntimeError("rollback access")
            for reader in (repo, public):
                reader._runtime(tx, project, root)
                reader._check_blob(tx, project, shared)

    archive_write(repo, project, write)


@pytest.mark.parametrize("kind", ["artifact", "runtime"])
def test_compiled_resources_include_only_requested_roots_after_unrelated_warm_reads(
    repo, scope, producer, kind
):
    project, public = scope[0], generation(repo)
    references, blobs = [], []
    for body in (b"SYNTHETIC unrelated A", b"SYNTHETIC requested B"):
        value = blob(repo, project, body)
        blobs.append(value)
        if kind == "runtime":
            ref = reference()
            repo.append_runtime(project, ref, dict(blob=value.model_dump(mode="json")))
        else:
            ref = repo.create(
                project,
                "Policy",
                dict(policy_kind="data", rules=dict(blob=value.model_dump(mode="json"))),
                producer=producer,
                operation_id=uuid4(),
            ).ref
        references.append(ref)
    unrelated, requested = references

    def write(tx):
        with authorized_read(public, tx):
            for reader in (repo, public):
                if kind == "runtime":
                    reader._runtime(tx, project, unrelated)
                else:
                    reader._authorize_artifact(tx, project, unrelated)
                reader._check_blob(tx, project, blobs[0])
            resources, _ = compile_resources(
                repo, tx, project, [dict(purpose="generation", ref=requested.model_dump(mode="json"))]
            )
            expected = str(requested.record_id if kind == "runtime" else requested.revision_id)
            for purpose in ("evaluation", "generation"):
                rows = [row for row in resources if row["purpose"] == purpose]
                assert {row["identity"] for row in rows if row["resource_kind"] == kind} == {expected}
                assert {row["identity"] for row in rows if row["resource_kind"] == "blob"} == {
                    blobs[1].storage_key
                }

    archive_write(repo, project, write)


def test_late_import_alias_reclassification_updates_both_views_without_primary_input_taint(repo, scope):
    project, public = scope[0], generation(repo)
    protected = blob(repo, project, b"SYNTHETIC protected shared SHA")
    derived = blob(repo, project, b"SYNTHETIC late forward derivative")
    primary = blob(repo, project, b"SYNTHETIC unrelated primary input")
    root, late, child, input_ref = (reference() for _ in range(4))
    repo.append_runtime(project, root, dict(blob=protected.model_dump(mode="json")), audience="evaluator")
    repo.append_runtime(project, input_ref, dict(blob=primary.model_dump(mode="json")))

    def write(tx):
        with authorized_read(public, tx):
            for reader in (repo, public):
                reader._check_blob(tx, project, derived)
                reader._runtime(tx, project, input_ref)
            # Legacy imports can create aliases after initial SHA classification.
            for ref, payload in (
                (late, dict(blob=protected.model_dump(mode="json"))),
                (
                    child,
                    dict(
                        parent=late.model_dump(mode="json"),
                        input=input_ref.model_dump(mode="json"),
                        blob=derived.model_dump(mode="json"),
                    ),
                ),
            ):
                query(
                    tx,
                    "INSERT INTO runtime_records VALUES(:p,:r,:k,CAST(:body AS json),'project')",
                    p=project,
                    r=ref.record_id,
                    k=ref.record_kind,
                    body=json_text(payload),
                )
            # Warm after INSERTs, so an unknown-mutation reset cannot substitute for classification reset.
            public._check_blob(tx, project, derived)
            classify_evaluator(tx, project, runtime_id=root.record_id)
            with pytest.raises(AccessDenied):
                public._check_blob(tx, project, derived)
            with pytest.raises(AccessDenied):
                public._runtime(tx, project, child)
            repo._runtime(tx, project, child)
            for reader in (repo, public):
                reader._runtime(tx, project, input_ref)
                reader._check_blob(tx, project, primary)

    archive_write(repo, project, write)


def test_late_semantic_links_require_source_authorization_after_cached_plain_runtime(
    private_scope, monkeypatch
):
    from podgen.persistence.private_journal import semantic_access

    case = private_scope
    command = Checkpoint(
        run_id=case.run_id,
        attempt_id=case.run_id,
        operation_id=uuid4(),
        kind="observation",
        input_set_id=case.bound["set_id"],
        expected_revision=0,
        data=dict(synthetic="semantic provenance source"),
    )
    commit(case.repo, case.project, command)
    result, payload = reference(), dict(output="SYNTHETIC semantic result")
    case.repo.append_runtime(case.project, result, payload, audience="evaluator")
    publication = replace(
        command,
        semantic_result=SemanticResult(result, payload, (command.entry_id,)),
    )
    checks, original = [], semantic_access.authorize

    def authorize(reader, tx, project, set_id):
        checks.append(set_id)
        return original(reader, tx, project, set_id)

    monkeypatch.setattr(semantic_access, "authorize", authorize)

    def write(tx):
        case.repo._runtime(tx, case.project, result)
        assert not checks
        publish_semantic(case.repo, tx, case.project, publication)
        checks.clear()
        case.repo._runtime(tx, case.project, result)
        assert checks == [case.bound["set_id"]]

    archive_write(case.repo, case.project, write)


def test_runtime_controls_added_after_insert_invalidate_incomplete_parent_privacy(exchange, monkeypatch):
    from podgen.persistence.execution import parents, store

    saved = exchange
    ref = reference()
    payload = dict(
        contract="model-invocation-event-v1",
        invocation_id=str(saved.invocation_id),
        event_kind="failure",
        request_ref=saved.root_request.model_dump(mode="json"),
        data=dict(
            source_turn_index=0,
            source_turn_id=str(saved.physical_id),
            turn_request_ref=saved.request.model_dump(mode="json"),
        ),
    )
    insert, one = store.insert_rows, parents.one
    checks = []

    def count(tx, sql, **params):
        if ref.record_id in params.get("records", ()):
            checks.append(sql)
        return one(tx, sql, **params)

    def warm_incomplete(tx, table, rows, **options):
        insert(tx, table, rows, **options)
        if table == "runtime_records" and any(row["record_id"] == ref.record_id for row in rows):
            saved.repo._runtime(tx, saved.project, ref)
            assert len(checks) == 1

    monkeypatch.setattr(parents, "one", count)
    monkeypatch.setattr(store, "insert_rows", warm_incomplete)

    def write(tx):
        store.append(
            saved.repo,
            tx,
            saved.project,
            saved.run_id,
            saved.bound["set_id"],
            [(ref, payload, False)],
            [],
        )
        # The pre-edge successful decision must not bypass the complete parent check.
        saved.repo._runtime(tx, saved.project, ref)
        assert len(checks) == 2

    archive_write(saved.repo, saved.project, write)


def test_direct_parent_privacy_cache_hit_after_savepoint_rollback_rechecks_withdrawal(
    exchange, producer, monkeypatch
):
    from podgen.persistence.execution import parents

    saved = exchange
    witness = event(saved.repo, saved.project, producer)
    original, checks = parents.one, []

    def count(tx, sql, **params):
        checks.append(sql)
        return original(tx, sql, **params)

    monkeypatch.setattr(parents, "one", count)

    def write(tx):
        with pytest.raises(RuntimeError, match="rollback parent check"):
            with tx.begin_nested():
                parents.authorize_parents(saved.repo, tx, saved.project, [saved.request.record_id])
                assert len(checks) == 1
                raise RuntimeError("rollback parent check")
        query(
            tx,
            """INSERT INTO execution_record_withdrawals(project_id,record_id,event_id,reason)
            VALUES(:p,:r,:event,'SYNTHETIC withdrawal after savepoint rollback')""",
            p=saved.project,
            r=saved.request.record_id,
            event=witness,
        )
        # No checked_node/access_facts call may repair the state before this direct fast path.
        with pytest.raises(AccessDenied):
            parents.authorize_parents(saved.repo, tx, saved.project, [saved.request.record_id])
        assert len(checks) == 2

    archive_write(saved.repo, saved.project, write)


@pytest.mark.parametrize("boundary", ["registered_sha", "propagated_sha"])
def test_private_checkpoint_protection_invalidates_warm_generic_alias_facts(
    private_scope, monkeypatch, boundary
):
    from podgen.persistence.private_journal import privacy, writer
    from podgen.persistence.private_journal.objects import prepare
    from podgen.persistence.publication_scope import owner_for

    case = private_scope
    public = generation(case.repo)
    body = b"SYNTHETIC new private checkpoint bytes with an existing public alias"
    shared = blob(case.repo, case.project, body)
    derived = blob(case.repo, case.project, b"SYNTHETIC public alias forward derivative")
    alias, child = reference(), reference()
    case.repo.append_runtime(case.project, alias, dict(blob=shared.model_dump(mode="json")))
    case.repo.append_runtime(
        case.project,
        child,
        dict(parent=alias.model_dump(mode="json"), blob=derived.model_dump(mode="json")),
    )
    command = Checkpoint(
        run_id=case.run_id,
        attempt_id=case.run_id,
        operation_id=uuid4(),
        kind="observation",
        input_set_id=case.bound["set_id"],
        expected_revision=0,
        data=dict(synthetic="first private protection"),
        objects=dict(body=prepare(body, case.project)),
    )
    checkpoints = []
    module, name, target = (
        (writer, "register", shared)
        if boundary == "registered_sha"
        else (privacy, "protect_private_owned", derived)
    )
    original = getattr(module, name)

    def protect(tx, project, entry_id, values):
        # Use the real commit transaction and mutation; only insert a read probe.
        assert owner_for(tx) is not None
        assert case.repo._read_authorization.get().transaction is tx
        with authorized_read(public, tx):
            public._check_blob(tx, project, target)
            returned = original(tx, project, entry_id, values)
            checkpoints.append(entry_id)
            with pytest.raises(AccessDenied):
                public._check_blob(tx, project, target)
            case.repo._check_blob(tx, project, target)
        return returned

    monkeypatch.setattr(module, name, protect)
    committed = commit(case.repo, case.project, command)
    assert committed["created"] and checkpoints == [command.entry_id]
    with pytest.raises(AccessDenied):
        public.authorize_runtime(case.project, child)
    case.repo.authorize_runtime(case.project, child)
    # First private ownership must not classify the borrowed generation source.
    assert public.get(case.project, case.source.ref).ref == case.source.ref
