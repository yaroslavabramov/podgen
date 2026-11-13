"""One snapshot-consistent SQL read for a bounded artifact metadata frontier."""

from .database import query

SQL = """
SELECT r.project_id,r.revision_id,r.artifact_id,r.kind,r.input_refs,r.audience,
    CASE WHEN r.kind='VoiceProfile' THEN r.payload->>'voice_kind' END AS catalog_voice_kind,
    jsonb_build_object(
        'edges',COALESCE((SELECT jsonb_agg(to_jsonb(e)) FROM artifact_dependencies e
            WHERE e.project_id=r.project_id AND e.consumer_id=r.revision_id),'[]'::jsonb),
        'events',COALESCE((SELECT jsonb_agg(d.payload::jsonb
            ORDER BY (d.payload->>'expected_validity_version')::bigint)
            FROM dependency_revalidations d WHERE d.project_id=r.project_id AND d.subject_id=r.revision_id
            AND (d.acceptance_record_id IS NULL OR NOT EXISTS(SELECT 1 FROM revocations v
                WHERE v.project_id=d.project_id AND v.acceptance_record_id=d.acceptance_record_id))),
            '[]'::jsonb),
        'privacy',COALESCE((SELECT jsonb_agg(l.input_id) FROM revalidation_privacy_links l
            WHERE l.project_id=r.project_id AND l.subject_id=r.revision_id),'[]'::jsonb),
        'blobs',COALESCE((SELECT jsonb_agg(to_jsonb(b)||jsonb_build_object(
            'binding_purpose',a.purpose,
            'access_revoked',EXISTS(SELECT 1 FROM blob_access_revocations v
                WHERE v.project_id=b.project_id AND v.sha256=b.sha256),
            'evaluator_only',EXISTS(SELECT 1 FROM evaluator_blob_classifications c
                WHERE c.project_id=b.project_id AND c.sha256=b.sha256)))
            FROM artifact_blobs a JOIN blobs b USING(project_id,storage_key)
            WHERE a.project_id=r.project_id AND a.revision_id=r.revision_id),'[]'::jsonb),
        'invalid',EXISTS(SELECT 1 FROM invalidations i WHERE i.project_id=r.project_id
            AND i.revision_id=r.revision_id AND i.category='access'),
        'runtimes',COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'record_id',l.record_id,'record_kind',l.record_kind)) FROM runtime_links l
            WHERE l.project_id=r.project_id AND l.revision_id=r.revision_id),'[]'::jsonb)
    ) AS access_graph
FROM artifact_revisions r WHERE r.revision_id=ANY(:ids)
"""


def load(tx, identities):
    return query(tx, SQL, ids=list(identities)).mappings()
