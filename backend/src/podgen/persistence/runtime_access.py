"""Frontier reads of typed runtime references, without retaining semantic payloads."""

from collections import defaultdict

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef

from .database import query

FRONTIER = 64


class RuntimeFacts:
    def __init__(self, tx, blobs, artifacts):
        self.tx, self.blobs, self.artifacts = tx, blobs, artifacts
        self.rows, self.references, self.missing = {}, {}, set()

    def prefetch(self, project, references):
        self._closure(project, [reference.record_id for reference in references])

    def row(self, project, reference):
        self._closure(project, [reference.record_id])
        row = self.rows.get((project, reference.record_id))
        return row if row is not None and row["record_kind"] == reference.record_kind else None

    def _closure(self, project, identities):
        pending = set(identities) - {identity for owner, identity in self.rows if owner == project}
        pending -= {identity for owner, identity in self.missing if owner == project}
        artifact_ids, blob_keys = set(), set()
        while pending:
            ids = list(pending)[:FRONTIER]
            pending.difference_update(ids)
            rows = list(
                query(
                    self.tx,
                    """SELECT project_id,record_id,record_kind,audience FROM runtime_records
                WHERE project_id=:p AND record_id=ANY(:ids)""",
                    p=project,
                    ids=ids,
                ).mappings()
            )
            self.rows.update(((project, row["record_id"]), row) for row in rows)
            self.missing.update(
                (project, identity) for identity in ids if (project, identity) not in self.rows
            )
            found = [row["record_id"] for row in rows]
            if not found:
                continue
            references = defaultdict(list)
            for value in query(
                self.tx,
                "SELECT record_id,artifact_id,revision_id,kind FROM runtime_artifact_refs WHERE project_id=:p AND record_id=ANY(:ids)",
                p=project,
                ids=found,
            ).mappings():
                reference = ArtifactRef(**{key: value[key] for key in ("artifact_id", "revision_id", "kind")})
                references[value["record_id"]].append(reference)
                artifact_ids.add(reference.revision_id)
            for value in query(
                self.tx,
                "SELECT record_id,target_record_id,record_kind FROM runtime_runtime_refs WHERE project_id=:p AND record_id=ANY(:ids)",
                p=project,
                ids=found,
            ).mappings():
                reference = RuntimeRef(record_id=value["target_record_id"], record_kind=value["record_kind"])
                references[value["record_id"]].append(reference)
                if (project, reference.record_id) not in self.rows and (
                    project,
                    reference.record_id,
                ) not in self.missing:
                    pending.add(reference.record_id)
            for value in query(
                self.tx,
                "SELECT record_id,storage_key,sha256,byte_length,media_type FROM runtime_blob_refs WHERE project_id=:p AND record_id=ANY(:ids)",
                p=project,
                ids=found,
            ).mappings():
                reference = BlobRef(
                    **{key: value[key] for key in ("storage_key", "sha256", "byte_length", "media_type")}
                )
                references[value["record_id"]].append(reference)
                blob_keys.add(reference.storage_key)
            self.references.update(((project, identity), references[identity]) for identity in found)
        self.artifacts.expand(artifact_ids)
        self.blobs.load(project, blob_keys)

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
