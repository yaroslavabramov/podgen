"""Forward privacy closure over indexed references; never over unrelated project payloads."""

from collections import deque
from itertools import islice
from uuid import UUID

from sqlalchemy import text

BATCH_SIZE = 512

# Existing artifact/canonical edges remain authoritative. Runtime reference tables
# are maintained by the DB on INSERT, including historical application writers.
EXPAND = text("""
SELECT 'artifact' AS node_kind, consumer_id::text AS identity
FROM revision_links WHERE project_id=:project AND input_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'artifact', subject_id::text FROM revalidation_privacy_links
WHERE project_id=:project AND input_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'runtime', record_id::text FROM runtime_artifact_refs
WHERE project_id=:project AND revision_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'runtime', record_id::text FROM runtime_runtime_refs
WHERE project_id=:project AND target_record_id=ANY(CAST(:runtimes AS uuid[]))
UNION
SELECT 'artifact', revision_id::text FROM runtime_links
WHERE project_id=:project AND record_id=ANY(CAST(:runtimes AS uuid[]))
UNION
SELECT 'blob', sha256 FROM runtime_blob_refs
WHERE project_id=:project AND record_id=ANY(CAST(:runtimes AS uuid[]))
UNION
SELECT 'blob', b.sha256 FROM artifact_blobs AS a JOIN blobs AS b USING(project_id,storage_key)
WHERE a.project_id=:project AND a.revision_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'blob', asset.value->'blob_ref'->>'sha256'
FROM catalog_publications AS p CROSS JOIN LATERAL jsonb_array_elements(p.config->'published_assets') AS asset
WHERE p.project_id=:project AND p.revision_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'runtime', record_id::text FROM runtime_blob_refs
WHERE project_id=:project AND sha256=ANY(CAST(:hashes AS text[]))
UNION
SELECT 'artifact', a.revision_id::text FROM blobs AS b JOIN artifact_blobs AS a USING(project_id,storage_key)
WHERE b.project_id=:project AND b.sha256=ANY(CAST(:hashes AS text[]))
UNION
SELECT 'artifact', p.revision_id::text
FROM unnest(CAST(:hashes AS text[])) AS marked(sha256) JOIN catalog_publications AS p
  ON p.project_id=:project AND p.config->'published_assets' @>
     jsonb_build_array(jsonb_build_object('blob_ref',jsonb_build_object('sha256',marked.sha256)))
""")


def protected_hashes(tx, project_id, *, artifact_ids=(), runtime_ids=()):
    """Preserve the complete forward+alias closure, including already classified hashes.

    Each reached identity is expanded once. Frontier queries and server cursor
    buffers are bounded; only reached IDs/hashes survive for this one operation.
    No permission decision or payload is cached between transactions.
    """
    reached = {
        "artifact": {UUID(str(value)) for value in artifact_ids},
        "runtime": {UUID(str(value)) for value in runtime_ids},
        "blob": set(),
    }
    pending = deque((kind, value) for kind, values in reached.items() for value in values)
    while pending:
        batch = list(islice(pending, BATCH_SIZE))
        for _ in batch:
            pending.popleft()
        args = dict(
            project=project_id,
            artifacts=[value for kind, value in batch if kind == "artifact"],
            runtimes=[value for kind, value in batch if kind == "runtime"],
            hashes=[value for kind, value in batch if kind == "blob"],
        )
        with tx.execute(EXPAND, args, execution_options={"yield_per": BATCH_SIZE}) as result:
            for row in result.mappings():
                kind = row["node_kind"]
                value = row["identity"] if kind == "blob" else UUID(row["identity"])
                if value not in reached[kind]:
                    reached[kind].add(value)
                    pending.append((kind, value))
    return reached["blob"]


def hash_batches(hashes):
    values = iter(sorted(hashes))
    while batch := list(islice(values, BATCH_SIZE)):
        yield batch
