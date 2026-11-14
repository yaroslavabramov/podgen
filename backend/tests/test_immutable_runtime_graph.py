"""Immutable metadata reuse cannot confer permissions or escape a rollback."""

from contextvars import ContextVar
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.database import query
from podgen.persistence.read_authorization import require_external_boundary
from podgen.persistence.repository import Repository
from podgen.persistence.runtime_graph import RuntimeGraph, runtime_graph
from podgen.persistence.runtime_records import append_runtime
from tests.authorization_helpers import withdraw_blob
from tests.test_read_authorization import diamond


def test_graph_has_hard_node_edge_bounds_and_exception_cleanup():
    graph = RuntimeGraph(max_nodes=2, max_edges=3)
    for _ in range(20):
        graph.remember(
            dict(project_id="p", record_id=uuid4(), record_kind="user_input", audience="project"),
            [RuntimeRef(record_kind="user_input", record_id=uuid4())] * 2,
        )
        assert len(graph.nodes) <= 2 and graph.edge_count <= 3
    graph.remember(
        dict(project_id="p", record_id=uuid4(), record_kind="user_input", audience="project"),
        [RuntimeRef(record_kind="user_input", record_id=uuid4())] * 4,
    )
    assert graph.edge_count <= 3
    repo = SimpleNamespace(_runtime_graph=ContextVar("test_graph", default=None))
    with pytest.raises(RuntimeError), runtime_graph(repo):
        owner = repo._runtime_graph.get()
        owner.remember(
            dict(project_id="p", record_id=uuid4(), record_kind="user_input", audience="project"), []
        )
        require_external_boundary()  # Immutable ownership carries no SQL/read lease.
        raise RuntimeError()
    assert repo._runtime_graph.get() is None and not owner.nodes


@pytest.mark.parametrize("revocation", ["blob", "membership"])
def test_only_topology_survives_fresh_public_reads(repo, scope, revocation):
    project, _ = scope
    root, _, blob, nodes = diamond(repo, project, depth=4)
    loads = []

    def capture(conn, cursor, sql, parameters, context, many):
        if "WITH RECURSIVE runtime_reach" in sql:
            loads.append(parameters)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        with runtime_graph(repo):
            repo.get_runtime(project, root)
            repo.get_runtime(project, root)
            assert len(loads) == 1
            owner = repo._runtime_graph.get()
            assert len(owner.nodes) == len(nodes)
            assert all(
                set(vars(node))
                == {
                    "project_id",
                    "record_id",
                    "record_kind",
                    "audience",
                    "references",
                    "execution_run_id",
                    "execution_input_set_id",
                    "reference_policy",
                }
                for node in owner.nodes.values()
            )
            if revocation == "blob":
                withdraw_blob(repo, project, blob)
            else:
                with repo.database.transaction() as tx:
                    query(
                        tx,
                        "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                        p=project,
                        a=repo.actor_id,
                    )
            with pytest.raises(AccessDenied):
                repo.get_runtime(project, root)
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert repo._runtime_graph.get() is None and not owner.nodes


def test_rollback_and_missing_nodes_never_publish_positive_or_negative_metadata(repo, scope):
    project, _ = scope
    ref = RuntimeRef(record_kind="user_input", record_id=uuid4())
    with runtime_graph(repo):
        with pytest.raises(AccessDenied):
            repo.get_runtime(project, ref)
        owner = repo._runtime_graph.get()
        assert not owner.nodes

        def rollback(tx):
            append_runtime(repo, tx, project, ref, dict(text="SYNTHETIC rolled back"))
            assert repo.get_runtime(project, ref)
            assert (project, ref.record_id) not in owner.nodes
            raise RuntimeError("rollback")

        with pytest.raises(RuntimeError, match="rollback"):
            archive_write(repo, project, rollback)
        with pytest.raises(AccessDenied):
            repo.get_runtime(project, ref)
        repo.append_runtime(project, ref, dict(text="SYNTHETIC committed"))
        assert repo.get_runtime(project, ref)["text"] == "SYNTHETIC committed"


def test_classification_changes_access_without_tainting_referenced_primary_input(repo, scope):
    project, _ = scope
    root, _, blob, _ = diamond(repo, project, depth=2)
    public = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    with runtime_graph(repo), runtime_graph(public):
        assert public.get_runtime(project, root)
        evaluator = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        repo.append_runtime(
            project, evaluator, dict(input_ref=root.model_dump(mode="json")), audience="evaluator"
        )
        assert public.get_runtime(project, root)  # Forward classification cannot flow into its input.
        direct = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        repo.append_runtime(project, direct, dict(blob=blob.model_dump(mode="json")), audience="evaluator")
        with pytest.raises(AccessDenied):
            public.get_runtime(project, root)
        assert repo.get_runtime(project, root)
