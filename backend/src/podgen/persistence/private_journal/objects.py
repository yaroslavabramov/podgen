"""Complete content-addressed byte staging; metadata is committed with its checkpoint."""

import hashlib
import io
from contextlib import ExitStack

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError
from podgen.persistence.read_authorization import require_external_boundary

from .contracts import PrivateBytes


def prepare(data, project, media_type="application/json"):
    checksum = hashlib.sha256(data).hexdigest()
    media = hashlib.sha256(media_type.encode()).hexdigest()
    ref = BlobRef(
        storage_key=f"{project}/content-v1/{checksum}/{media}",
        sha256=checksum,
        byte_length=len(data),
        media_type=media_type,
    )
    return PrivateBytes(ref, data)


def upload(repo, project, values):
    require_external_boundary()
    if len(values) > 64:
        raise ContractError("private_object_batch_too_large")
    with ExitStack() as stack:
        staged = [
            stack.enter_context(
                repo.objects.stage(
                    project,
                    io.BytesIO(value.body),
                    value.reference.media_type,
                    expected_sha256=value.reference.sha256,
                    expected_length=value.reference.byte_length,
                )
            )
            for value in values
        ]
        for value, item in zip(values, staged, strict=True):
            ref = repo.objects.publish(project, item)
            if ref != value.reference:
                raise ContractError("private_object_identity_mismatch")


def register(tx, project, entry_id, references):
    """Register at most one bounded checkpoint, returning only first private SHAs."""
    from podgen.domain.errors import Conflict
    from podgen.persistence.database import query

    references = list(dict.fromkeys(references))
    if not references:
        return set()
    params, rows = dict(project=project, entry=entry_id), []
    for index, ref in enumerate(references):
        params.update(
            {
                f"{key}{index}": value
                for key, value in dict(
                    sha=ref.sha256,
                    media=ref.media_type,
                    length=ref.byte_length,
                    key=ref.storage_key,
                ).items()
            }
        )
        rows.append(f"(:project,:sha{index},:media{index},:length{index},:key{index},:entry)")
    # Read existing catalog identities once while registering the same bounded
    # batch. This removes the second catalog scan, not just a client round-trip.
    catalog = list(
        query(
            tx,
            f"""WITH prior AS MATERIALIZED (
        SELECT * FROM execution_objects WHERE project_id=:project
        AND sha256=ANY(CAST(:hashes AS text[]))
    ), added AS (
        INSERT INTO execution_objects(project_id,sha256,media_type,byte_length,storage_key,first_entry_id)
        VALUES {",".join(rows)} ON CONFLICT DO NOTHING RETURNING *
    ) SELECT *,false AS inserted FROM prior UNION ALL SELECT *,true AS inserted FROM added""",
            **params,
            hashes=[ref.sha256 for ref in references],
        ).mappings()
    )
    prior_hashes = {row["sha256"] for row in catalog if not row["inserted"]}
    fresh = {row["sha256"] for row in catalog if row["inserted"]} - prior_hashes
    if fresh:
        from podgen.persistence.publication_scope import AccessChange, notify

        notify(tx, AccessChange())
    stored = {(row["sha256"], row["media_type"]): row for row in catalog}
    if any(
        (row := stored.get((ref.sha256, ref.media_type))) is None
        or row["byte_length"] != ref.byte_length
        or row["storage_key"] != ref.storage_key
        for ref in references
    ):
        raise Conflict("private_object_reused_with_different_identity")
    return fresh
