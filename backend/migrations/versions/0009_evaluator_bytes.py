"""Purpose ACL follows gold bytes, including legacy wrappers and byte deduplication."""

from uuid import UUID

from alembic import op
from sqlalchemy import text

revision = "0009_evaluator_bytes"
down_revision = "0008_privacy_backfill"
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
    op.execute("""CREATE TABLE evaluator_blob_classifications(
      project_id uuid NOT NULL, sha256 text NOT NULL, artifact_revision_id uuid, runtime_record_id uuid,
      CHECK((artifact_revision_id IS NULL) <> (runtime_record_id IS NULL)),
      FOREIGN KEY(project_id,artifact_revision_id) REFERENCES artifact_revisions(project_id,revision_id),
      FOREIGN KEY(project_id,runtime_record_id) REFERENCES runtime_records(project_id,record_id)
    )""")
    op.execute(
        "CREATE UNIQUE INDEX evaluator_bytes_artifact ON evaluator_blob_classifications(project_id,sha256,artifact_revision_id) WHERE artifact_revision_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX evaluator_bytes_runtime ON evaluator_blob_classifications(project_id,sha256,runtime_record_id) WHERE runtime_record_id IS NOT NULL"
    )
    op.execute("CREATE INDEX evaluator_bytes_lookup ON evaluator_blob_classifications(project_id,sha256)")
    op.execute(
        "CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON evaluator_blob_classifications FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
    )
    tx = op.get_bind()

    def rows(sql, **params):
        return list(tx.execute(text(sql), params).mappings())

    projects = rows(
        "SELECT DISTINCT project_id FROM artifact_revisions WHERE audience='evaluator' UNION SELECT DISTINCT project_id FROM runtime_records WHERE audience='evaluator'"
    )
    for project_row in projects:
        project = project_row["project_id"]
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
        roots = [
            ("artifact", r["revision_id"])
            for r in rows(
                "SELECT revision_id FROM artifact_revisions WHERE project_id=:p AND audience='evaluator'",
                p=project,
            )
        ]
        roots += [
            ("runtime", r["record_id"])
            for r in rows(
                "SELECT record_id FROM runtime_records WHERE project_id=:p AND audience='evaluator'",
                p=project,
            )
        ]
        for kind, identity in roots:
            artifacts, runtimes, hashes = (
                ({identity} if kind == "artifact" else set()),
                ({identity} if kind == "runtime" else set()),
                set(),
            )
            changed = True
            while changed:
                before = (len(artifacts), len(runtimes), len(hashes))
                for link in links:
                    if link["input_id"] in artifacts:
                        artifacts.add(link["consumer_id"])
                for rid, values in runtime.items():
                    if any(
                        k == "artifact"
                        and v in artifacts
                        or k == "runtime"
                        and v in runtimes
                        or k == "blob"
                        and v in hashes
                        for k, v in values
                    ):
                        runtimes.add(rid)
                    if rid in runtimes:
                        hashes.update(v for k, v in values if k == "blob")
                for link in runtime_links:
                    if link["record_id"] in runtimes:
                        artifacts.add(link["revision_id"])
                for aid, checksum in blobs:
                    if aid in artifacts:
                        hashes.add(checksum)
                    if checksum in hashes:
                        artifacts.add(aid)
                changed = before != (len(artifacts), len(runtimes), len(hashes))
            for checksum in hashes:
                tx.execute(
                    text(
                        "INSERT INTO evaluator_blob_classifications VALUES(:p,:sha,:a,:r) ON CONFLICT DO NOTHING"
                    ),
                    dict(
                        p=project,
                        sha=checksum,
                        a=identity if kind == "artifact" else None,
                        r=identity if kind == "runtime" else None,
                    ),
                )


def downgrade():
    raise RuntimeError("Evaluator-only byte classification must survive schema changes.")
