"""Small independent fixed-point oracle for exact historical privacy semantics."""

from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class Graph:
    original: set = field(default_factory=set)  # (consumer artifact, input artifact)
    revalidation: set = field(default_factory=set)
    runtime_artifacts: set = field(default_factory=set)  # (runtime, input artifact)
    runtime_inputs: set = field(default_factory=set)  # (runtime, input runtime)
    artifact_runtimes: set = field(default_factory=set)  # (artifact, input runtime)
    runtime_blobs: set = field(default_factory=set)
    artifact_blobs: set = field(default_factory=set)
    catalog_blobs: set = field(default_factory=set)

    def legacy_closure(self, artifacts=(), runtimes=()):
        artifacts, runtimes, hashes = set(artifacts), set(runtimes), set()
        while True:
            before = (len(artifacts), len(runtimes), len(hashes))
            artifacts.update(c for c, source in self.original | self.revalidation if source in artifacts)
            runtimes.update(r for r, source in self.runtime_artifacts if source in artifacts)
            runtimes.update(r for r, source in self.runtime_inputs if source in runtimes)
            runtimes.update(r for r, sha in self.runtime_blobs if sha in hashes)
            hashes.update(sha for r, sha in self.runtime_blobs if r in runtimes)
            artifacts.update(a for a, source in self.artifact_runtimes if source in runtimes)
            owners = self.artifact_blobs | self.catalog_blobs
            hashes.update(sha for a, sha in owners if a in artifacts)
            artifacts.update(a for a, sha in owners if sha in hashes)
            if before == (len(artifacts), len(runtimes), len(hashes)):
                return hashes

    def expand(self, args):
        found = {}
        for source_kind, input_key, target_kind, edges in (
            ("artifact", "artifacts", "artifact", self.original | self.revalidation),
            ("artifact", "artifacts", "runtime", self.runtime_artifacts),
            ("runtime", "runtimes", "runtime", self.runtime_inputs),
            ("runtime", "runtimes", "artifact", self.artifact_runtimes),
            ("runtime", "runtimes", "blob", {(sha, r) for r, sha in self.runtime_blobs}),
            (
                "artifact",
                "artifacts",
                "blob",
                {(sha, a) for a, sha in self.artifact_blobs | self.catalog_blobs},
            ),
            ("blob", "hashes", "runtime", self.runtime_blobs),
            ("blob", "hashes", "artifact", self.artifact_blobs | self.catalog_blobs),
        ):
            frontier = set(args[input_key])
            for target, source in edges:
                if source in frontier:
                    found.setdefault((target_kind, target), (source_kind, source))
        return [
            dict(node_kind=k, identity=str(i), source_kind=source_kind, source_identity=str(source))
            for (k, i), (source_kind, source) in found.items()
        ]


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def mappings(self):
        return iter(self.rows)


class GraphConnection:
    def __init__(self, graph):
        self.graph, self.expansions, self.writes = graph, [], []

    def execute(self, statement, params, execution_options=None):
        if str(statement).lstrip().startswith("INSERT"):
            self.writes.append((str(statement), params))
            return Rows([])
        assert execution_options == {"yield_per": 512}
        self.expansions.append(params)
        return Rows(self.graph.expand(params))


def identity(number):
    return UUID(int=number)


def checksum(number):
    return f"{number:064x}"
