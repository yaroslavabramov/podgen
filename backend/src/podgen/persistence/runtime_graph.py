"""Invocation-owned immutable topology, never cached permission decisions or payloads."""

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeNode:
    project_id: object
    record_id: object
    record_kind: str
    audience: str
    references: tuple
    execution_run_id: object | None = None
    execution_input_set_id: object | None = None

    def header(self):
        return {
            key: getattr(self, key)
            for key in (
                "project_id",
                "record_id",
                "record_kind",
                "audience",
                "execution_run_id",
                "execution_input_set_id",
            )
        }


class RuntimeGraph:
    def __init__(self, *, max_nodes=4096, max_edges=32768):
        self.nodes = OrderedDict()
        self.max_nodes, self.max_edges, self.edge_count = max_nodes, max_edges, 0

    def remember(self, row, references):
        node = RuntimeNode(**row, references=tuple(references))
        key = node.project_id, node.record_id
        previous = self.nodes.pop(key, None)
        if previous is not None:
            self.edge_count -= len(previous.references)
        if len(node.references) > self.max_edges:
            return  # Cache admission never truncates a record or its references.
        self.nodes[key] = node
        self.edge_count += len(node.references)
        while len(self.nodes) > self.max_nodes or self.edge_count > self.max_edges:
            _, removed = self.nodes.popitem(last=False)
            self.edge_count -= len(removed.references)

    def clear(self):
        self.nodes.clear()
        self.edge_count = 0


@contextmanager
def runtime_graph(repo):
    """Share INSERT-only headers/typed refs for this invocation, across fresh snapshots.

    No missing-node result, actor role, blob status, artifact disposition, successful
    authorization, SQL connection or semantic body enters this owner. Database
    history triggers make positive runtime nodes and their reference projection
    immutable. Every new ReadAuthorization still owns all live access decisions.
    """
    if repo._runtime_graph.get() is not None:
        yield
        return
    graph = RuntimeGraph()
    token = repo._runtime_graph.set(graph)
    try:
        yield
    finally:
        repo._runtime_graph.reset(token)
        graph.clear()
