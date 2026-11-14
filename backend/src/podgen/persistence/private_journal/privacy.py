"""First private byte ownership closes only preexisting generic alias components."""

from podgen.persistence.classification import _mark
from podgen.persistence.database import query
from podgen.persistence.privacy_graph import hash_batches, protected_alias_origins


def protect_private_owned(tx, project, entry_id, fresh_hashes):
    """Called after entry/object INSERT under the shared project write fence.

    Private own bytes are protected by execution_objects itself. Only existing
    generic aliases can require additional SHA protection with this real entry
    as witness. Neither borrowed input bytes nor unrelated history are visited.
    An empty first-ownership set performs no query or traversal.
    """
    from podgen.persistence.publication_scope import AccessChange, notify

    for hashes in hash_batches(fresh_hashes):
        aliases = set(
            query(
                tx,
                """SELECT sha256 FROM runtime_blob_refs
            WHERE project_id=:p AND sha256=ANY(CAST(:hashes AS text[]))
            UNION
            SELECT b.sha256 FROM artifact_blobs a JOIN blobs b USING(project_id,storage_key)
            WHERE a.project_id=:p AND b.sha256=ANY(CAST(:hashes AS text[]))
            UNION
            SELECT marked.sha FROM unnest(CAST(:hashes AS text[])) AS marked(sha)
            JOIN catalog_publications pub ON pub.project_id=:p
              AND pub.config->'published_assets' @> jsonb_build_array(
                jsonb_build_object('blob_ref',jsonb_build_object('sha256',marked.sha)))""",
                p=project,
                hashes=hashes,
            ).scalars()
        )
        if aliases:
            origins = protected_alias_origins(tx, project, dict.fromkeys(aliases, entry_id))
            if _mark(tx, project, origins, execution=True):
                notify(tx, AccessChange())
