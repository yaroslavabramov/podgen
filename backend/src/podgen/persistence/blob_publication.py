"""Publish verified bytes only after fresh authorization; no network inside SQL transactions."""

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError

from .batch_insert import insert_rows
from .blob_access import validate_identity
from .database import one, query


def check_identity(repo, tx, project_id, sha256, *, public_only=False):
    from .blob_access import validate_identity

    facts = one(
        tx,
        """SELECT
        EXISTS(SELECT 1 FROM evaluator_blob_classifications WHERE project_id=:p AND sha256=:sha) AS evaluator_only,
        EXISTS(SELECT 1 FROM blob_access_revocations WHERE project_id=:p AND sha256=:sha) AS access_revoked""",
        p=project_id,
        sha=sha256,
    )
    role = (
        repo._acl(tx, project_id)
        if facts["evaluator_only"] and not public_only and repo.purpose == "evaluation"
        else None
    )
    validate_identity(**dict(facts), purpose=repo.purpose, role=role, public_only=public_only)


def save_blob(repo, project_id, stream, media_type, **kwargs):
    with repo.database.transaction() as tx:
        repo._acl(tx, project_id, write=True)
    with repo.objects.stage(project_id, stream, media_type, **kwargs) as staged:
        return publish_staged(repo, project_id, [staged])[0]


def _check_batch(repo, tx, project, staged):
    role = repo._acl(tx, project, write=True)
    for row in query(
        tx,
        """SELECT sha AS sha256,
        EXISTS(SELECT 1 FROM evaluator_blob_classifications c
               WHERE c.project_id=:p AND c.sha256=sha) AS evaluator_only,
        EXISTS(SELECT 1 FROM blob_access_revocations v
               WHERE v.project_id=:p AND v.sha256=sha) AS access_revoked
        FROM unnest(CAST(:hashes AS text[])) AS inputs(sha)""",
        p=project,
        hashes=list({item.sha256 for item in staged}),
    ).mappings():
        validate_identity(
            evaluator_only=row["evaluator_only"],
            access_revoked=row["access_revoked"],
            purpose=repo.purpose,
            role=role,
        )


def _lookup_batch(tx, project, staged):
    found = {}
    for row in query(
        tx,
        """SELECT * FROM blobs WHERE project_id=:p
        AND sha256=ANY(CAST(:hashes AS text[])) AND media_type=ANY(CAST(:media AS text[]))""",
        p=project,
        hashes=list({item.sha256 for item in staged}),
        media=list({item.media_type for item in staged}),
    ).mappings():
        found[row["sha256"], row["media_type"]] = BlobRef(
            **{key: row[key] for key in ("storage_key", "sha256", "byte_length", "media_type")}
        )
    return found


def publish_staged(repo, project, staged):
    """Shared scalar/batch policy. Every object operation is outside SQL."""
    if not staged or len(staged) > 64:
        raise ContractError("invalid_blob_publication_size")
    with repo.database.transaction() as tx:
        _check_batch(repo, tx, project, staged)
        existing = _lookup_batch(tx, project, staged)
    published = {}
    for item in staged:
        key = (item.sha256, item.media_type)
        if key in existing:
            item.matches(existing[key])
            repo.objects.verify_existing(project, existing[key], item)
            published[key] = existing[key]
        else:
            published[key] = repo.objects.publish(project, item)
    with repo.database.transaction() as tx:
        # Same fresh post-upload check as scalar publication, including missing
        # identities: a revoked SHA can never be resurrected under a new key.
        _check_batch(repo, tx, project, staged)
        insert_rows(
            tx,
            "blobs",
            [
                dict(
                    project_id=project,
                    storage_key=ref.storage_key,
                    sha256=ref.sha256,
                    byte_length=ref.byte_length,
                    media_type=ref.media_type,
                )
                for key, ref in published.items()
                if key not in existing
            ],
            on_conflict=True,
        )
        winners = _lookup_batch(tx, project, staged)
        for item in staged:
            winner = winners.get((item.sha256, item.media_type))
            if winner is None:
                raise ContractError("blob_publication_missing")
            item.matches(winner)
    changed = [
        item
        for item in staged
        if winners[item.sha256, item.media_type] != published[item.sha256, item.media_type]
    ]
    if changed:
        for item in changed:
            repo.objects.verify_existing(project, winners[item.sha256, item.media_type], item)
        with repo.database.transaction() as tx:
            _check_batch(repo, tx, project, staged)
    return [winners[item.sha256, item.media_type] for item in staged]
