"""Shared writer identity, lineage and bounded multirow behavior."""

from uuid import uuid4

from sqlalchemy import event

from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.artifact_batch import publish
from podgen.persistence.publication_plan import ArtifactNode, PublicationBatch


def test_more_than_64_nodes_remain_one_atomic_publication_with_bounded_statements(repo, scope, producer):
    nodes = tuple(
        ArtifactNode(str(i), "Policy", dict(policy_kind="data", rules=dict(index=i)), producer, uuid4())
        for i in range(130)
    )
    counts = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO artifact_revisions"):
            counts.append(len(parameters))

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        batch = PublicationBatch(artifacts=nodes)
        first = archive_write(repo, scope[0], lambda tx: publish(repo, tx, scope[0], batch))
        second = archive_write(repo, scope[0], lambda tx: publish(repo, tx, scope[0], batch))
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert first == second and len(first) == 130
    assert len(counts) == 3 and max(counts) <= 64 * 15
