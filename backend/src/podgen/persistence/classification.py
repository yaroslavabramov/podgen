"""Protect new owned bytes directly; close existing aliases only on first protection."""

from .database import query
from .privacy_graph import hash_batches, protected_alias_origins, protected_hash_origins, protected_hashes
from .publication_scope import access_mutation


def _mark(tx, project, origins, *, artifact=False, execution=False):
    column = (
        "execution_entry_id" if execution else "artifact_revision_id" if artifact else "runtime_record_id"
    )
    count = 0
    for batch in hash_batches(origins):
        count += query(
            tx,
            f"""INSERT INTO evaluator_blob_classifications(project_id,sha256,{column})
            SELECT :p,sha,origin FROM unnest(CAST(:hashes AS text[]),CAST(:origins AS uuid[])) AS bytes(sha,origin)
            WHERE NOT EXISTS(SELECT 1 FROM protected_byte_hashes known
                WHERE known.project_id=:p AND known.sha256=bytes.sha)
            ORDER BY sha ON CONFLICT DO NOTHING""",
            p=project,
            hashes=batch,
            origins=[origins[sha] for sha in batch],
        ).rowcount
    return count


@access_mutation
def _classify(tx, project, *, artifact_id=None, runtime_ids=()):
    """Legacy/import publication preserves historical forward-reference closure.

    ExecutionStore exclusively uses protect_owned; ordinary receipts never enter
    this semantic writer, including its support for late imported aliases.
    """
    origins = (
        dict.fromkeys(protected_hashes(tx, project, artifact_ids=[artifact_id]), artifact_id)
        if artifact_id is not None
        else protected_hash_origins(tx, project, runtime_ids)
    )
    _mark(tx, project, origins, artifact=artifact_id is not None)


def protect_owned(tx, project, owned, *, artifact=False):
    """Only new owned SHA protection can require existing forward aliases."""
    if not owned:
        return
    known = (
        set(
            query(
                tx,
                """SELECT sha256 FROM protected_byte_hashes
        WHERE project_id=:p AND sha256=ANY(CAST(:hashes AS text[]))""",
                p=project,
                hashes=list(owned),
            ).scalars()
        )
        if owned
        else set()
    )
    fresh = {sha: owner for sha, owner in owned.items() if sha not in known}
    if not fresh:
        return
    from .publication_scope import AccessChange, notify

    try:
        _mark(tx, project, fresh, artifact=artifact)
        aliases = set(
            query(
                tx,
                """
            SELECT sha256 FROM runtime_blob_refs WHERE project_id=:p
             AND sha256=ANY(CAST(:hashes AS text[])) AND NOT(record_id=ANY(CAST(:runtimes AS uuid[])))
            UNION
            SELECT b.sha256 FROM artifact_blobs a JOIN blobs b USING(project_id,storage_key)
             WHERE a.project_id=:p AND b.sha256=ANY(CAST(:hashes AS text[]))
             AND (CAST(:artifact AS uuid) IS NULL OR a.revision_id<>CAST(:artifact AS uuid))
            UNION
            SELECT marked.sha FROM unnest(CAST(:hashes AS text[])) marked(sha)
            JOIN catalog_publications pub ON pub.project_id=:p
             AND pub.config->'published_assets' @> jsonb_build_array(jsonb_build_object('blob_ref',jsonb_build_object('sha256',marked.sha)))
            WHERE CAST(:artifact AS uuid) IS NULL OR pub.revision_id<>CAST(:artifact AS uuid)
            """,
                p=project,
                hashes=list(fresh),
                runtimes=[] if artifact else list(set(owned.values())),
                artifact=next(iter(owned.values())) if artifact else None,
            ).scalars()
        )
        if aliases:
            propagated = protected_alias_origins(tx, project, {sha: fresh[sha] for sha in aliases})
            _mark(tx, project, propagated, artifact=artifact)
    finally:
        notify(tx, AccessChange())


def classify_evaluator_batch(tx, project, runtime_ids):
    _classify(tx, project, runtime_ids=runtime_ids)


def classify_evaluator(tx, project, *, artifact_id=None, runtime_id=None):
    _classify(tx, project, artifact_id=artifact_id, runtime_ids=[runtime_id] if runtime_id else ())
