"""Backfill earlier access withdrawals and enforce source identity for all new revisions.

The graph algorithm here is frozen migration code, independent of the current application.
"""

from uuid import UUID

from alembic import op
from sqlalchemy import text

revision = "0008_privacy_backfill"
down_revision = "0007_mutation_receipts"
branch_labels = None
depends_on = None


def refs(value):
    if isinstance(value, dict):
        if set(value) == {"artifact_id", "revision_id", "kind"}:
            yield ("artifact", UUID(value["revision_id"]))
        elif set(value) == {"record_kind", "record_id"}:
            yield ("runtime", UUID(value["record_id"]))
        elif set(value) == {"storage_key", "sha256", "byte_length", "media_type"}:
            yield ("blob", value["sha256"])
        else:
            for child in value.values():
                yield from refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from refs(child)


def upgrade():
    tx = op.get_bind()

    def rows(sql, **params):
        return list(tx.execute(text(sql), params).mappings())

    events = rows("SELECT * FROM invalidations WHERE category='access'")
    for event in events:
        project = event["project_id"]
        links = rows("SELECT consumer_id,input_id FROM revision_links WHERE project_id=:p", p=project)
        links += rows(
            "SELECT subject_id AS consumer_id,input_id FROM revalidation_privacy_links WHERE project_id=:p",
            p=project,
        )
        runtime = {
            r["record_id"]: list(refs(r["payload"]))
            for r in rows("SELECT record_id,payload FROM runtime_records WHERE project_id=:p", p=project)
        }
        runtime_links = rows("SELECT revision_id,record_id FROM runtime_links WHERE project_id=:p", p=project)
        blobs = [
            (r["revision_id"], r["sha256"])
            for r in rows(
                "SELECT a.revision_id,b.sha256 FROM artifact_blobs a JOIN blobs b USING(project_id,storage_key) WHERE a.project_id=:p",
                p=project,
            )
        ]
        blobs += [
            (r["revision_id"], asset["blob_ref"]["sha256"])
            for r in rows(
                "SELECT revision_id,config FROM catalog_publications WHERE project_id=:p", p=project
            )
            for asset in r["config"]["published_assets"]
        ]
        artifacts, runtimes, hashes = {event["revision_id"]}, set(), set()
        changed = True
        while changed:
            before = (len(artifacts), len(runtimes), len(hashes))
            for link in links:
                if link["input_id"] in artifacts:
                    artifacts.add(link["consumer_id"])
            for identity, values in runtime.items():
                if any(
                    kind == "artifact"
                    and value in artifacts
                    or kind == "runtime"
                    and value in runtimes
                    or kind == "blob"
                    and value in hashes
                    for kind, value in values
                ):
                    runtimes.add(identity)
                if identity in runtimes:
                    hashes.update(value for kind, value in values if kind == "blob")
            for link in runtime_links:
                if link["record_id"] in runtimes:
                    artifacts.add(link["revision_id"])
            for identity, checksum in blobs:
                if identity in artifacts:
                    hashes.add(checksum)
                if checksum in hashes:
                    artifacts.add(identity)
            changed = before != (len(artifacts), len(runtimes), len(hashes))
        for checksum in hashes:
            tx.execute(
                text(
                    "INSERT INTO blob_access_revocations VALUES(:p,:sha,:id,:reason) ON CONFLICT DO NOTHING"
                ),
                dict(p=project, sha=checksum, id=event["event_id"], reason=event["reason"]),
            )
    # Historical immutable test data is retained. New writes must satisfy the invariant.
    op.execute(
        "ALTER TABLE artifact_revisions ADD CONSTRAINT source_identity_matches CHECK(kind<>'SourceRevision' OR (payload->>'source_id')::uuid=artifact_id) NOT VALID"
    )


def downgrade():
    raise RuntimeError("Access withdrawal and source identity constraints cannot be weakened.")
