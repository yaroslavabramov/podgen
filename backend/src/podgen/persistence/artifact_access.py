"""Bounded artifact frontiers and complete privacy reachability within one snapshot."""

from collections import defaultdict, deque
from uuid import UUID

from podgen.domain.contracts.common import ArtifactRef, BlobRef

from .dependencies import apply_dispositions

FRONTIER = 64


class ArtifactFacts:
    def __init__(self, tx, blobs, *, publication=None):
        self.tx, self.blobs = tx, blobs
        self.publication = publication
        self.reset()

    def reset(self):
        self.rows, self.edges, self.bindings, self.privacy_links = {}, {}, {}, {}
        self.runtime_links, self.integrity_blobs = {}, {}
        self.missing, self.blocked, self.expanded = set(), set(), set()
        self._pending_denials = deque()
        self.runtime_frontiers = deque()
        self.children, self.parents, self.local_children = {}, defaultdict(set), {}

    def row(self, reference):
        self.load([reference.revision_id])
        row = self.rows.get(reference.revision_id)
        if row is None or row["artifact_id"] != reference.artifact_id or row["kind"] != reference.kind:
            return None
        return row

    def load(self, identities):
        if self.publication is not None:
            self.publication.sync()
        identities = tuple(identities)
        pending = [
            identity
            for identity in set(identities)
            if identity not in self.rows and identity not in self.missing
        ]
        for offset in range(0, len(pending), FRONTIER):
            self._load_frontier(pending[offset : offset + FRONTIER])

    def _load_frontier(self, identities):
        from .artifact_frontier import load

        loaded = list(load(self.tx, identities))
        missing = set(identities) - {row["revision_id"] for row in loaded}
        self.missing.update(missing)
        self._block(missing)
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
                self._block([identity])
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
                    self._block([identity])
            self.runtime_links[identity] = [
                dict(record_id=UUID(link["record_id"]), record_kind=link["record_kind"])
                for link in graph["runtimes"]
            ]
            self.runtime_frontiers.append((row["project_id"], self.runtime_links[identity]))
            children = {ArtifactRef.model_validate(value).revision_id for value in row["input_refs"]}
            children.update(edge["input_id"] for edge in self.edges[identity])
            children.update(links)
            self.local_children[identity] = {edge["input_id"] for edge in self.edges[identity]} | links
            self.children[identity] = children
            for child in children:
                self.parents[child].add(identity)
                if child in self.blocked:
                    self._block([identity])
        self._propagate()

    def _block(self, identities):
        for identity in identities:
            if identity not in self.blocked:
                self.blocked.add(identity)
                self._pending_denials.append(identity)

    def _propagate(self):
        while self._pending_denials:
            self._block(self.parents[self._pending_denials.popleft()])

    def expand(self, identities):
        if self.publication is not None:
            self.publication.sync()
        pending, visited = set(identities) - self.expanded, set()
        if not pending:
            return
        while pending:
            frontier = pending - visited
            if not frontier:
                break
            self.load(frontier)
            visited.update(frontier)
            pending = {
                child
                for identity in frontier
                for child in self.children.get(identity, ())
                if child not in visited and child not in self.expanded
            }
        for parent in visited:
            owner = self.rows.get(parent, {}).get("project_id")
            if any(child in self.blocked for child in self.children.get(parent, ())):
                self._block([parent])
            if any(
                self.rows.get(child, {}).get("project_id") != owner
                for child in self.local_children.get(parent, ())
            ):
                self._block([parent])
        self._propagate()
        self.expanded.update(visited)

    def access_references(self, row):
        """Declared, read-authorized dependencies, without scanning semantic payload.

        The immutable artifact writer records all payload/producer/input artifact
        refs in input_refs, all payload blob refs in artifact_blobs and all payload
        runtime refs in runtime_links. Effective edges add current provenance.
        Inactive privacy-only links are not read grants and are not traversed here.
        """
        identity = row["revision_id"]
        references = [ArtifactRef.model_validate(value) for value in row["input_refs"]]
        for edge in self.edges[identity]:
            child = self.rows[edge["input_id"]]
            references.append(
                ArtifactRef(**{key: child[key] for key in ("artifact_id", "revision_id", "kind")})
            )
        from podgen.domain.contracts.common import RuntimeRef

        references.extend(
            RuntimeRef(record_id=value["record_id"], record_kind=value["record_kind"])
            for value in self.runtime_links[identity]
        )
        references.extend(self.bindings[identity])
        return tuple(dict.fromkeys(references))

    def invalid(self, row):
        self.expand([row["revision_id"]])
        return row["revision_id"] in self.blocked
