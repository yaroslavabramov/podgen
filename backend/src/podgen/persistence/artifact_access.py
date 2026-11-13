"""Bounded artifact frontiers and complete privacy reachability within one snapshot."""

from collections import defaultdict, deque

from podgen.domain.contracts.common import ArtifactRef, BlobRef

from .database import query
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
        rows = list(
            query(
                self.tx, "SELECT * FROM artifact_revisions WHERE revision_id=ANY(:ids)", ids=identities
            ).mappings()
        )
        self.rows.update((row["revision_id"], row) for row in rows)
        missing = set(identities) - self.rows.keys()
        self.missing.update(missing)
        self.blocked.update(missing)
        if not rows:
            return
        ids = [row["revision_id"] for row in rows]
        projects = list({row["project_id"] for row in rows})
        edges, events, links, bindings = (
            defaultdict(list),
            defaultdict(list),
            defaultdict(list),
            defaultdict(list),
        )
        for edge in query(
            self.tx,
            "SELECT * FROM artifact_dependencies WHERE project_id=ANY(:projects) AND consumer_id=ANY(:ids)",
            projects=projects,
            ids=ids,
        ).mappings():
            edges[edge["consumer_id"]].append(edge)
        for item in query(
            self.tx,
            """SELECT d.subject_id,d.payload FROM dependency_revalidations d
            WHERE project_id=ANY(:projects) AND subject_id=ANY(:ids)
            AND (d.acceptance_record_id IS NULL OR NOT EXISTS(SELECT 1 FROM revocations v
                WHERE v.project_id=d.project_id AND v.acceptance_record_id=d.acceptance_record_id))
            ORDER BY (payload->>'expected_validity_version')::bigint""",
            projects=projects,
            ids=ids,
        ).mappings():
            events[item["subject_id"]].append(item)
        for link in query(
            self.tx,
            "SELECT subject_id,input_id FROM revalidation_privacy_links WHERE project_id=ANY(:projects) AND subject_id=ANY(:ids)",
            projects=projects,
            ids=ids,
        ).mappings():
            links[link["subject_id"]].append(link["input_id"])
        integrity_blobs = defaultdict(list)
        for blob in query(
            self.tx,
            """SELECT a.revision_id,a.purpose AS binding_purpose,b.*,
            EXISTS(SELECT 1 FROM blob_access_revocations v WHERE v.project_id=b.project_id AND v.sha256=b.sha256) AS access_revoked,
            EXISTS(SELECT 1 FROM evaluator_blob_classifications c WHERE c.project_id=b.project_id AND c.sha256=b.sha256) AS evaluator_only
            FROM artifact_blobs a JOIN blobs b USING(project_id,storage_key)
            WHERE a.project_id=ANY(:projects) AND a.revision_id=ANY(:ids)""",
            projects=projects,
            ids=ids,
        ).mappings():
            self.blobs.rows[blob["project_id"], blob["storage_key"]] = blob
            integrity_blobs[blob["revision_id"]].append(
                dict(purpose=blob["binding_purpose"], hash=blob["sha256"])
            )
            bindings[blob["revision_id"]].append(
                BlobRef(
                    storage_key=blob["storage_key"],
                    sha256=blob["sha256"],
                    byte_length=blob["byte_length"],
                    media_type=blob["media_type"],
                )
            )
            if blob["access_revoked"]:
                self.blocked.add(blob["revision_id"])
        for item in query(
            self.tx,
            "SELECT revision_id FROM invalidations WHERE project_id=ANY(:projects) AND revision_id=ANY(:ids) AND category='access'",
            projects=projects,
            ids=ids,
        ).mappings():
            self.blocked.add(item["revision_id"])
        runtime_links = defaultdict(list)
        for link in query(
            self.tx,
            "SELECT revision_id,record_id,record_kind FROM runtime_links WHERE project_id=ANY(:projects) AND revision_id=ANY(:ids)",
            projects=projects,
            ids=ids,
        ).mappings():
            runtime_links[link["revision_id"]].append(link)
        for row in rows:
            identity = row["revision_id"]
            self.runtime_links[identity] = runtime_links[identity]
            self.integrity_blobs[identity] = integrity_blobs[identity]
            self.edges[identity] = apply_dispositions(edges[identity], events[identity])
            self.bindings[identity] = bindings[identity]
            self.privacy_links[identity] = links[identity]
            children = {ArtifactRef.model_validate(value).revision_id for value in row["input_refs"]}
            children.update(edge["input_id"] for edge in self.edges[identity])
            children.update(links[identity])
            self.local_children[identity] = {edge["input_id"] for edge in self.edges[identity]} | set(
                links[identity]
            )
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
