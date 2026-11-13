"""One indexed metadata closure; successful loading is never an access decision."""

from .database import query

# UNION bounds cycles by identity. The caller still validates every typed edge,
# principal, audience and active-path cycle through ScopeAccess before returning.
# Already loaded nodes have complete snapshot-local closures and need no reload.
SQL = """
WITH RECURSIVE runtime_reach(record_id) AS (
    SELECT unnest(CAST(:ids AS uuid[]))
    UNION
    SELECT edge.target_record_id
    FROM runtime_runtime_refs edge JOIN runtime_reach parent ON edge.record_id=parent.record_id
    WHERE edge.project_id=:p AND NOT(edge.target_record_id=ANY(CAST(:known AS uuid[])))
)
SELECT r.project_id,r.record_id,r.record_kind,r.audience,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'artifact_id',a.artifact_id,'revision_id',a.revision_id,'kind',a.kind))
        FROM runtime_artifact_refs a WHERE a.project_id=r.project_id AND a.record_id=r.record_id),
        '[]'::jsonb) AS artifact_references,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'record_id',a.target_record_id,'record_kind',a.record_kind))
        FROM runtime_runtime_refs a WHERE a.project_id=r.project_id AND a.record_id=r.record_id),
        '[]'::jsonb) AS runtime_references,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'storage_key',a.storage_key,'sha256',a.sha256,'byte_length',a.byte_length,'media_type',a.media_type))
        FROM runtime_blob_refs a WHERE a.project_id=r.project_id AND a.record_id=r.record_id),
        '[]'::jsonb) AS blob_references
FROM runtime_records r JOIN runtime_reach n ON r.record_id=n.record_id
WHERE r.project_id=:p AND NOT(r.record_id=ANY(CAST(:known AS uuid[])))
"""


def load(tx, project, identities, known):
    return query(tx, SQL, p=project, ids=list(identities), known=list(known)).mappings()
