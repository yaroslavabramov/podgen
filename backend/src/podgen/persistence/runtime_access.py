"""Frontier reads of typed runtime references, without retaining semantic payloads."""

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef

from .database import query


class RuntimeFacts:
    def __init__(self, tx, blobs, artifacts, *, graph=None, cache_writes=False):
        self.tx, self.blobs, self.artifacts, self.graph = tx, blobs, artifacts, graph
        self.rows, self.references, self.missing = {}, {}, set()
        self.semantic_sources = {}
        # Only the owned READ ONLY transaction can publish positive metadata.
        # A writable archive transaction may reuse committed metadata, but its
        # inserts must never escape a rollback through this invocation owner.
        self.cache_writes = cache_writes

    def prefetch(self, project, references):
        self._closure(project, [reference.record_id for reference in references])

    def row(self, project, reference):
        self._closure(project, [reference.record_id])
        row = self.rows.get((project, reference.record_id))
        return row if row is not None and row["record_kind"] == reference.record_kind else None

    def _closure(self, project, identities):
        from .runtime_frontier import load

        graph = self.graph
        pending = {
            identity
            for identity in identities
            if (project, identity) not in self.rows and (project, identity) not in self.missing
        }
        if not pending:
            return
        artifact_ids, blob_keys = set(), set()
        while pending:
            requested = pending
            pending = set()
            unknown = set()
            for identity in requested:
                key = project, identity
                if key in self.rows or key in self.missing:
                    continue
                node = graph.nodes.get(key) if graph is not None else None
                if node is None:
                    unknown.add(identity)
                else:
                    self.rows[key] = node.header()
                    self.references[key] = node.references
                    if node.execution_input_set_id is None:
                        pending.update(
                            ref.record_id for ref in node.references if isinstance(ref, RuntimeRef)
                        )
            if not unknown:
                continue
            known = {identity for owner, identity in self.rows if owner == project}
            if graph is not None:
                known.update(identity for owner, identity in graph.nodes if owner == project)
            found = set()
            for row in load(self.tx, project, unknown, known):
                key = project, row["record_id"]
                header = {
                    field: row[field] for field in ("project_id", "record_id", "record_kind", "audience")
                }
                header.update(
                    {field: row.get(field) for field in ("execution_run_id", "execution_input_set_id")}
                )
                references = [ArtifactRef.model_validate(value) for value in row["artifact_references"]]
                if header["execution_input_set_id"] is None:
                    references.extend(RuntimeRef.model_validate(value) for value in row["runtime_references"])
                references.extend(BlobRef.model_validate(value) for value in row["blob_references"])
                self.rows[key], self.references[key] = header, references
                found.add(row["record_id"])
                if header["execution_input_set_id"] is None:
                    pending.update(ref.record_id for ref in references if isinstance(ref, RuntimeRef))
                if graph is not None and self.cache_writes:
                    graph.remember(header, references)
            self.missing.update((project, identity) for identity in unknown - found)
        # Live blob/role/privacy facts are loaded into this snapshot only, even
        # when every immutable runtime edge came from the invocation owner.
        for (owner, _), references in self.references.items():
            if owner != project:
                continue
            artifact_ids.update(ref.revision_id for ref in references if isinstance(ref, ArtifactRef))
            blob_keys.update(ref.storage_key for ref in references if isinstance(ref, BlobRef))
        self.artifacts.expand(artifact_ids)
        self.blobs.load(project, blob_keys)
        self._semantic_sources(project)

    def _semantic_sources(self, project):
        pending = [
            identity
            for owner, identity in self.rows
            if owner == project and (owner, identity) not in self.semantic_sources
        ]
        for offset in range(0, len(pending), 64):
            batch = pending[offset : offset + 64]
            for identity in batch:
                self.semantic_sources[project, identity] = []
            for row in query(
                self.tx,
                """SELECT l.runtime_record_id,e.entry_id,e.input_set_id,
                EXISTS(SELECT 1 FROM execution_entry_denials d
                    WHERE d.project_id=e.project_id AND d.entry_id=e.entry_id)
                OR EXISTS(SELECT 1 FROM blob_access_revocations v
                    WHERE v.project_id=e.project_id AND e.owned_hashes @> ARRAY[v.sha256]) AS denied
                FROM semantic_execution_links l JOIN execution_entries e USING(project_id,entry_id)
                WHERE l.project_id=:p AND l.runtime_record_id=ANY(CAST(:ids AS uuid[]))""",
                p=project,
                ids=batch,
            ).mappings():
                self.semantic_sources[project, row["runtime_record_id"]].append(dict(row))

    def payload(self, project, reference):
        # Caller authorizes the node first. A full body is fetched only for an
        # explicit public read and is never retained in authorization facts.
        return query(
            self.tx,
            "SELECT payload FROM runtime_records WHERE project_id=:p AND record_id=:r AND record_kind=:k",
            p=project,
            r=reference.record_id,
            k=reference.record_kind,
        ).scalar_one()
