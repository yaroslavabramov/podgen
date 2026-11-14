"""Forward privacy closure over indexed references; never over unrelated project payloads."""

from collections import deque
from itertools import islice
from uuid import UUID

from sqlalchemy import text

BATCH_SIZE = 512

# Existing artifact/canonical edges remain authoritative. Runtime reference tables
# are maintained by the DB on INSERT, including historical application writers.
EXPAND = text("""
SELECT DISTINCT ON (node_kind, identity) * FROM (
SELECT 'artifact' AS node_kind, consumer_id::text AS identity, 'artifact' AS source_kind, input_id::text AS source_identity
FROM revision_links WHERE project_id=:project AND input_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'artifact', subject_id::text, 'artifact', input_id::text FROM revalidation_privacy_links
WHERE project_id=:project AND input_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'runtime', record_id::text, 'artifact', revision_id::text FROM runtime_artifact_refs
WHERE project_id=:project AND revision_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'runtime', record_id::text, 'runtime', target_record_id::text FROM runtime_runtime_refs
WHERE project_id=:project AND target_record_id=ANY(CAST(:runtimes AS uuid[]))
UNION
SELECT 'artifact', revision_id::text, 'runtime', record_id::text FROM runtime_links
WHERE project_id=:project AND record_id=ANY(CAST(:runtimes AS uuid[]))
UNION
SELECT 'blob', sha256, 'runtime', record_id::text FROM runtime_blob_refs
WHERE project_id=:project AND record_id=ANY(CAST(:runtimes AS uuid[])) AND is_owned
UNION
SELECT 'blob', b.sha256, 'artifact', a.revision_id::text FROM artifact_blobs AS a JOIN blobs AS b USING(project_id,storage_key)
WHERE a.project_id=:project AND a.revision_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'blob', asset.value->'blob_ref'->>'sha256', 'artifact', p.revision_id::text
FROM catalog_publications AS p CROSS JOIN LATERAL jsonb_array_elements(p.config->'published_assets') AS asset
WHERE p.project_id=:project AND p.revision_id=ANY(CAST(:artifacts AS uuid[]))
UNION
SELECT 'runtime', record_id::text, 'blob', sha256 FROM runtime_blob_refs
WHERE project_id=:project AND sha256=ANY(CAST(:hashes AS text[]))
UNION
SELECT 'artifact', a.revision_id::text, 'blob', b.sha256 FROM blobs AS b JOIN artifact_blobs AS a USING(project_id,storage_key)
WHERE b.project_id=:project AND b.sha256=ANY(CAST(:hashes AS text[]))
UNION
SELECT 'artifact', p.revision_id::text, 'blob', marked.sha256
FROM unnest(CAST(:hashes AS text[])) AS marked(sha256) JOIN catalog_publications AS p
  ON p.project_id=:project AND p.config->'published_assets' @>
     jsonb_build_array(jsonb_build_object('blob_ref',jsonb_build_object('sha256',marked.sha256)))
UNION
SELECT 'input_set',set_id::text,'artifact',identity FROM execution_input_resources
WHERE project_id=:project AND owner_project_id=:project AND privacy_required
 AND resource_kind='artifact' AND identity=ANY(CAST(CAST(:artifacts AS uuid[]) AS text[]))
UNION
SELECT 'input_set',set_id::text,'runtime',identity FROM execution_input_resources
WHERE project_id=:project AND owner_project_id=:project AND privacy_required
 AND resource_kind='runtime' AND identity=ANY(CAST(CAST(:runtimes AS uuid[]) AS text[]))
UNION
SELECT 'input_set',set_id::text,'blob',binding->>'sha256' FROM execution_input_resources
WHERE project_id=:project AND owner_project_id=:project AND privacy_required
 AND resource_kind='blob' AND binding->>'sha256'=ANY(CAST(:hashes AS text[]))
UNION
SELECT 'runtime',record_id::text,'input_set',execution_input_set_id::text FROM runtime_records
WHERE project_id=:project AND execution_input_set_id=ANY(CAST(:input_sets AS uuid[]))
UNION
SELECT 'entry',entry_id::text,'input_set',input_set_id::text FROM execution_entries
WHERE project_id=:project AND input_set_id=ANY(CAST(:input_sets AS uuid[]))
UNION
SELECT 'entry',e.entry_id::text,'entry',parent.entry_id::text
FROM unnest(CAST(:entries AS uuid[])) AS parent(entry_id)
JOIN execution_entries e ON e.project_id=:project AND e.parent_entries @> ARRAY[parent.entry_id]
UNION
SELECT 'blob',owned.sha256,'entry',e.entry_id::text FROM execution_entries e
CROSS JOIN LATERAL unnest(e.owned_hashes) AS owned(sha256)
WHERE e.project_id=:project AND e.entry_id=ANY(CAST(:entries AS uuid[]))
UNION
SELECT 'entry',e.entry_id::text,'blob',marked.sha256
FROM unnest(CAST(:hashes AS text[])) AS marked(sha256)
JOIN execution_entries e ON e.project_id=:project AND e.owned_hashes @> ARRAY[marked.sha256]
UNION
SELECT 'runtime',runtime_record_id::text,'entry',entry_id::text FROM semantic_execution_links
WHERE project_id=:project AND entry_id=ANY(CAST(:entries AS uuid[])) AND runtime_record_id IS NOT NULL
UNION
SELECT 'artifact',artifact_revision_id::text,'entry',entry_id::text FROM semantic_execution_links
WHERE project_id=:project AND entry_id=ANY(CAST(:entries AS uuid[])) AND artifact_revision_id IS NOT NULL
) AS frontier ORDER BY node_kind, identity, source_kind, source_identity
""")


def privacy_closure(tx, project_id, *, artifact_ids=(), runtime_ids=(), hash_origins=None):
    """Expand each reached node once and retain one genuinely reaching root."""
    origins = {
        (kind, UUID(str(value))): UUID(str(value))
        for kind, values in (("artifact", artifact_ids), ("runtime", runtime_ids))
        for value in values
    }
    origins.update({("blob", sha): origin for sha, origin in (hash_origins or {}).items()})
    pending = deque(origins)
    while pending:
        batch = list(islice(pending, BATCH_SIZE))
        for _ in batch:
            pending.popleft()
        args = dict(
            project=project_id,
            artifacts=[value for kind, value in batch if kind == "artifact"],
            runtimes=[value for kind, value in batch if kind == "runtime"],
            hashes=[value for kind, value in batch if kind == "blob"],
            input_sets=[value for kind, value in batch if kind == "input_set"],
            entries=[value for kind, value in batch if kind == "entry"],
        )
        with tx.execute(EXPAND, args, execution_options={"yield_per": BATCH_SIZE}) as result:
            for row in result.mappings():
                kind, source = row["node_kind"], row["source_kind"]
                value = row["identity"] if kind == "blob" else UUID(row["identity"])
                previous = row["source_identity"] if source == "blob" else UUID(row["source_identity"])
                key = (kind, value)
                if key not in origins:
                    origins[key] = origins[source, previous]
                    pending.append(key)
    return origins


def _protected_origins(tx, project_id, **roots):
    origins = privacy_closure(tx, project_id, **roots)
    return {value: origin for (kind, value), origin in origins.items() if kind == "blob"}


def protected_hashes(tx, project_id, *, artifact_ids=(), runtime_ids=()):
    return set(_protected_origins(tx, project_id, artifact_ids=artifact_ids, runtime_ids=runtime_ids))


def protected_hash_origins(tx, project_id, runtime_ids):
    return _protected_origins(tx, project_id, runtime_ids=runtime_ids)


def hash_batches(hashes):
    values = iter(sorted(hashes))
    while batch := list(islice(values, BATCH_SIZE)):
        yield batch


def protected_alias_origins(tx, project_id, origins):
    return _protected_origins(tx, project_id, hash_origins=origins)
