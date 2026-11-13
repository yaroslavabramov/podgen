"""Immutable evaluator-only byte classification, distinct from privacy withdrawal."""

from .database import query
from .privacy_graph import hash_batches, protected_hashes


def classify_evaluator(tx, project, *, artifact_id=None, runtime_id=None):
    hashes = protected_hashes(
        tx,
        project,
        artifact_ids=[artifact_id] if artifact_id else (),
        runtime_ids=[runtime_id] if runtime_id else (),
    )
    for batch in hash_batches(hashes):
        query(
            tx,
            """INSERT INTO evaluator_blob_classifications(project_id,sha256,artifact_revision_id,runtime_record_id)
               SELECT :p,checksum,:a,:r FROM unnest(CAST(:hashes AS text[])) AS marked(checksum)
               WHERE NOT EXISTS(SELECT 1 FROM evaluator_protected_hashes AS known
                                WHERE known.project_id=:p AND known.sha256=marked.checksum)
               ORDER BY checksum
               ON CONFLICT DO NOTHING""",
            p=project,
            hashes=batch,
            a=artifact_id,
            r=runtime_id,
        )
