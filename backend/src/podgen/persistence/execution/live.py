"""Live conditions over a prepared set; no provenance traversal or permission cache."""

from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one
from podgen.persistence.read_authorization import checked_node


class StaleInputs(RuntimeError):
    def __init__(self, project, set_id):
        self.project, self.set_id = project, set_id
        super().__init__("execution_input_expansion_stale")


SQL = """
WITH expansion AS (
 SELECT owner_versions FROM execution_input_expansions WHERE project_id=:p AND set_id=:s
), freshness AS (
 SELECT count(*)>0 AND bool_and(COALESCE(p.validity_version=owner.value::bigint,false)) AS fresh
 FROM expansion CROSS JOIN LATERAL jsonb_each_text(owner_versions) owner
 LEFT JOIN projects p ON p.project_id=owner.key::uuid
), conditions AS (
 SELECT r.*,CASE WHEN :action THEN r.purpose ELSE :reader END AS check_purpose,
   m.role,
   a.revision_id AS artifact_found, a.artifact_id, a.kind AS artifact_kind,
   runtime.record_id AS runtime_found, runtime.record_kind AS runtime_kind,
   b.storage_key AS blob_found,b.sha256,b.byte_length,b.media_type,
   pub.publication_id AS publication_found,pub.config
 FROM execution_input_resources r
 LEFT JOIN project_members m ON m.project_id=r.owner_project_id AND m.actor_id=:actor
 LEFT JOIN artifact_revisions a ON r.resource_kind='artifact'
   AND a.project_id=r.owner_project_id AND a.revision_id=CASE WHEN r.resource_kind='artifact' THEN r.identity::uuid END
 LEFT JOIN runtime_records runtime ON r.resource_kind='runtime'
   AND runtime.project_id=r.owner_project_id AND runtime.record_id=CASE WHEN r.resource_kind='runtime' THEN r.identity::uuid END
 LEFT JOIN blobs b ON r.resource_kind='blob'
   AND b.project_id=r.owner_project_id AND b.storage_key=r.identity
 LEFT JOIN catalog_publications pub ON r.resource_kind='catalog'
   AND pub.project_id=r.owner_project_id AND pub.publication_id=CASE WHEN r.resource_kind='catalog' THEN r.identity::uuid END
 WHERE r.project_id=:p AND r.set_id=:s AND (:action OR r.purpose=:reader)
)
SELECT (SELECT fresh FROM freshness) AS fresh,
 NOT EXISTS(SELECT 1 FROM conditions c WHERE CASE c.resource_kind
 WHEN 'membership' THEN c.role IS NULL OR (c.identity='evaluator' AND (c.check_purpose<>'evaluation' OR c.role NOT IN ('owner','evaluator','operator')))
 WHEN 'artifact' THEN c.artifact_found IS NULL
   OR c.artifact_id::text IS DISTINCT FROM c.binding->>'artifact_id'
   OR c.artifact_kind IS DISTINCT FROM c.binding->>'kind'
   OR EXISTS(SELECT 1 FROM invalidations i WHERE i.project_id=c.owner_project_id
      AND i.revision_id=c.artifact_found AND i.category='access')
 WHEN 'runtime' THEN c.runtime_found IS NULL OR c.runtime_kind IS DISTINCT FROM c.binding->>'record_kind'
   OR EXISTS(SELECT 1 FROM execution_record_withdrawals w WHERE w.project_id=c.owner_project_id AND w.record_id=c.runtime_found)
   OR EXISTS(SELECT 1 FROM semantic_execution_links l JOIN execution_entries e USING(project_id,entry_id)
      WHERE l.project_id=c.owner_project_id AND l.runtime_record_id=c.runtime_found
        AND (EXISTS(SELECT 1 FROM execution_entry_denials d
          WHERE d.project_id=e.project_id AND d.entry_id=e.entry_id)
        OR EXISTS(SELECT 1 FROM blob_access_revocations v
          WHERE v.project_id=e.project_id AND v.sha256=ANY(e.owned_hashes))))
 WHEN 'blob' THEN c.blob_found IS NULL OR c.sha256 IS DISTINCT FROM c.binding->>'sha256'
   OR c.byte_length IS DISTINCT FROM (c.binding->>'byte_length')::bigint
   OR c.media_type IS DISTINCT FROM c.binding->>'media_type'
   OR EXISTS(SELECT 1 FROM blob_access_revocations v WHERE v.project_id=c.owner_project_id
      AND v.sha256=c.binding->>'sha256')
   OR (c.read_required AND EXISTS(SELECT 1 FROM protected_byte_hashes e
      WHERE e.project_id=c.owner_project_id AND e.sha256=c.binding->>'sha256')
      AND (c.check_purpose<>'evaluation' OR c.role IS NULL OR c.role NOT IN ('owner','evaluator','operator')))
 WHEN 'catalog' THEN c.publication_found IS NULL
   OR NOT(c.config->'allowed_consumer_project_ids' ? (c.binding->>'consumer_project_id'))
   OR EXISTS(SELECT 1 FROM catalog_revocations v WHERE v.publication_id=c.publication_found)
 ELSE false END) AS allowed
"""


def authorize(repo, tx, project, set_id, *, action=False):
    purpose = None if action else repo.purpose

    def check():
        row = one(tx, SQL, p=project, s=set_id, actor=repo.actor_id, action=action, reader=repo.purpose)
        if not row or not row["fresh"]:
            raise StaleInputs(project, set_id)
        if not row["allowed"]:
            raise AccessDenied()
        return True

    return checked_node(repo, tx, ("execution_inputs", project, set_id, purpose), check)
