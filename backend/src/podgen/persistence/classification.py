"""Immutable evaluator-only byte classification, distinct from privacy withdrawal."""

from .database import query
from .privacy import protected_hashes


def classify_evaluator(tx, project, *, artifact_id=None, runtime_id=None):
    hashes = protected_hashes(
        tx,
        project,
        artifact_ids=[artifact_id] if artifact_id else (),
        runtime_ids=[runtime_id] if runtime_id else (),
    )
    for checksum in hashes:
        query(
            tx,
            """INSERT INTO evaluator_blob_classifications(project_id,sha256,artifact_revision_id,runtime_record_id)
                    VALUES(:p,:sha,:a,:r) ON CONFLICT DO NOTHING""",
            p=project,
            sha=checksum,
            a=artifact_id,
            r=runtime_id,
        )
