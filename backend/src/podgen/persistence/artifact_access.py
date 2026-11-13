"""Bounded artifact frontiers and complete privacy reachability within one snapshot."""

from collections import defaultdict, deque
from uuid import UUID

from podgen.domain.contracts.common import ArtifactRef, BlobRef

from .dependencies import apply_dispositions

FRONTIER = 64


class ArtifactFacts:
    def __init__(self, tx, blobs):
        self.tx, self.blobs = tx, blobs
        self.rows, self.edges, self.bindings, self.privacy_links = {}, {}, {}, {}
        self.runtime_links, self.integrity_blobs = {}, {}
        self.missing, self.blocked = set(), set()
        self.children, self.parents, self.local_children = {}, defaultdict(set), {}

    def row(self, reference):
        self.load([reference.revision_id])
        row = self.rows.get(reference.revision_id)
        if row is None or row["artifact_id"] != reference.artifact_id or row["kind"] != reference.kind:
            return None
        return row

    def load(self, identities):
        pending = list(set(identities) - self.rows.keys() - self.missing)
        for offset in range(0, len(pending), FRONTIER):
            self._load_frontier(pending[offset : offset + FRONTIER])

    def _load_frontier(self, identities):
        from .artifact_frontier import load

        loaded = list(load(self.tx, identities))
        missing = set(identities) - {row["revision_id"] for row in loaded}
        self.missing.update(missing)
        self.blocked.update(missing)
        for value in loaded:
            row = dict(value)
            graph = row.pop("access_graph")
            identity = row["revision_id"]
            self.rows[identity] = row
            edges = [
                dict(edge, **{key: UUID(edge[key]) for key in ("project_id", "consumer_id", "input_id")})
                for edge in graph["edges"]
            ]
            self.edges[identity] = apply_dispositions(
                edges, [dict(payload=payload) for payload in graph["events"]]
            )
            links = {UUID(link) for link in graph["privacy"]}
            self.privacy_links[identity] = links
            self.bindings[identity], self.integrity_blobs[identity] = [], []
            if graph["invalid"]:
                self.blocked.add(identity)
            for blob in graph["blobs"]:
                blob["project_id"] = UUID(blob["project_id"])
                self.blobs.rows[blob["project_id"], blob["storage_key"]] = blob
                self.integrity_blobs[identity].append(
                    dict(purpose=blob["binding_purpose"], hash=blob["sha256"])
                )
                self.bindings[identity].append(
                    BlobRef(
                        **{key: blob[key] for key in ("storage_key", "sha256", "byte_length", "media_type")}
                    )
                )
                if blob["access_revoked"]:
                    self.blocked.add(identity)
            self.runtime_links[identity] = [
                dict(record_id=UUID(link["record_id"]), record_kind=link["record_kind"])
                for link in graph["runtimes"]
            ]
            children = {ArtifactRef.model_validate(value).revision_id for value in row["input_refs"]}
            children.update(edge["input_id"] for edge in self.edges[identity])
            children.update(links)
            self.local_children[identity] = {edge["input_id"] for edge in self.edges[identity]} | links
            self.children[identity] = children
            for child in children:
                self.parents[child].add(identity)
        self._propagate()

    def _propagate(self):
        pending = deque(self.blocked)
        while pending:
            for parent in self.parents[pending.popleft()] - self.blocked:
                self.blocked.add(parent)
                pending.append(parent)

    def expand(self, identities):
        pending, visited = set(identities), set()
        while pending:
            frontier = pending - visited
            if not frontier:
                break
            self.load(frontier)
            visited.update(frontier)
            pending = {child for identity in frontier for child in self.children.get(identity, ())} - visited
        for parent in visited:
            owner = self.rows.get(parent, {}).get("project_id")
            if any(
                self.rows.get(child, {}).get("project_id") != owner
                for child in self.local_children.get(parent, ())
            ):
                self.blocked.add(parent)
        self._propagate()
        return visited

    def invalid(self, row):
        self.expand([row["revision_id"]])
        return row["revision_id"] in self.blocked
