"""Privacy follows provenance AND aliases of identical bytes. Quality never restores access."""

from .database import query
from .privacy_graph import hash_batches, protected_hashes


def revoke_content(tx, project_id, revision_id, event_id, reason):
    for hashes in hash_batches(protected_hashes(tx, project_id, artifact_ids=[revision_id])):
        query(
            tx,
            """INSERT INTO blob_access_revocations(project_id,sha256,event_id,reason)
               SELECT :p,checksum,:id,:reason FROM unnest(CAST(:hashes AS text[])) AS marked(checksum)
               ON CONFLICT DO NOTHING""",
            p=project_id,
            hashes=hashes,
            id=event_id,
            reason=reason,
        )
