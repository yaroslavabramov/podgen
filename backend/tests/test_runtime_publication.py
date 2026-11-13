"""Atomic phases preserve strict authorization and have bounded publication work."""

from io import BytesIO
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.database import one
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.privacy_graph import protected_hash_origins
from podgen.persistence.runtime_publication import append_evaluation_batch


def reference():
    return RuntimeRef(record_kind="tool_execution", record_id=uuid4())


def publish(repo, project, records):
    archive_write(repo, project, lambda tx: append_evaluation_batch(repo, tx, project, records))


def test_validation_is_union_before_insert_and_classification_is_union(repo, scope, monkeypatch):
    from podgen.persistence import runtime_publication

    project, _ = scope
    original, validations = runtime_publication._validate_inputs, []

    def validate(*args):
        validations.append(args[3])
        return original(*args)

    monkeypatch.setattr(runtime_publication, "_validate_inputs", validate)
    blob = repo.save_blob(project, BytesIO(b"complete uncut input"), "text/plain")
    root, records = reference(), []
    records.append((root, dict(blob=blob.model_dump(mode="json")), False))
    for _ in range(31):
        records.append((reference(), dict(parent=root.model_dump(mode="json")), False))
    statements = []

    def capture(conn, cursor, sql, parameters, context, many):
        assert not many, "publication must use actual multi-row SQL"
        statements.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        publish(repo, project, records)
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert len(validations) == 1 and validations[0] == [blob.model_dump(mode="json")]
    assert sum(sql.startswith("INSERT INTO runtime_records") for sql in statements) == 1
    assert sum("INSERT INTO evaluator_blob_classifications" in sql for sql in statements) == 1
    assert repo.get_runtime(project, records[-1][0])["parent"] == root.model_dump(mode="json")


def test_invalid_late_dependency_publishes_nothing(repo, scope):
    project, _ = scope
    first, second = reference(), reference()
    with pytest.raises(AccessDenied):
        publish(
            repo,
            project,
            [
                (first, dict(full="first value"), False),
                (second, dict(missing=reference().model_dump(mode="json")), False),
            ],
        )
    with repo.database.transaction() as tx:
        assert (
            one(
                tx,
                "SELECT 1 FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=project,
                r=first.record_id,
            )
            is None
        )
    with pytest.raises(ContractError, match="invalid_runtime_publication_dependency"):
        publish(
            repo,
            project,
            [
                (first, dict(later=second.model_dump(mode="json")), False),
                (second, {}, False),
            ],
        )


def test_union_classification_records_a_reaching_root_for_independent_and_diamond_inputs(repo, scope):
    project, _ = scope
    blobs = [repo.save_blob(project, BytesIO(value), "text/plain") for value in (b"first", b"second")]
    first, second, left, right, child = [reference() for _ in range(5)]
    publish(
        repo,
        project,
        [
            (first, dict(blob=blobs[0].model_dump(mode="json")), False),
            (second, dict(blob=blobs[1].model_dump(mode="json")), False),
            (left, dict(parent=first.model_dump(mode="json")), False),
            (right, dict(parent=first.model_dump(mode="json")), False),
            (
                child,
                dict(
                    left=left.model_dump(mode="json"),
                    right=right.model_dump(mode="json"),
                    same_sha=blobs[0].model_dump(mode="json"),
                ),
                False,
            ),
        ],
    )
    with repo.database.transaction() as tx:
        origins = protected_hash_origins(tx, project, [first.record_id, second.record_id])
        assert origins == {blobs[0].sha256: first.record_id, blobs[1].sha256: second.record_id}
        for blob, root in zip(blobs, (first, second), strict=True):
            classified = one(
                tx,
                """SELECT runtime_record_id FROM evaluator_blob_classifications
                WHERE project_id=:p AND sha256=:sha""",
                p=project,
                sha=blob.sha256,
            )
            # A seed consumer with identical bytes is also a valid reaching root.
            assert classified["runtime_record_id"] in (
                {first.record_id, child.record_id} if root == first else {second.record_id}
            )


def test_replay_is_still_subject_to_fresh_withdrawal(repo, scope, producer):
    project, _ = scope
    blob = repo.save_blob(project, BytesIO(b"withdrawn"), "text/plain")
    owner = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(blob=blob.model_dump(mode="json"))),
        producer=producer,
        operation_id=uuid4(),
    )
    ref, payload = reference(), dict(blob=blob.model_dump(mode="json"))
    publish(repo, project, [(ref, payload, False)])
    DependencyLedger(repo).invalidate(project, owner.ref, reason="withdrawn", category="access")
    with pytest.raises(AccessDenied):
        publish(repo, project, [(ref, payload, True)])
