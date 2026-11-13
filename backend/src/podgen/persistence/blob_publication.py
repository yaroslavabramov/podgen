"""Publish verified bytes only after fresh authorization; no network inside SQL transactions."""

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError

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


def lookup(tx, project_id, staged):
    row = one(
        tx,
        "SELECT * FROM blobs WHERE project_id=:p AND sha256=:sha AND media_type=:m",
        p=project_id,
        sha=staged.sha256,
        m=staged.media_type,
    )
    if row is None:
        return None
    ref = BlobRef(**{k: row[k] for k in ("storage_key", "sha256", "byte_length", "media_type")})
    staged.matches(ref)
    return ref


def save_blob(repo, project_id, stream, media_type, **kwargs):
    with repo.database.transaction() as tx:
        repo._acl(tx, project_id, write=True)
    with repo.objects.stage(project_id, stream, media_type, **kwargs) as staged:
        with repo.database.transaction() as tx:
            repo._acl(tx, project_id, write=True)
            check_identity(repo, tx, project_id, staged.sha256)
            existing = lookup(tx, project_id, staged)
        if existing is not None:
            repo.objects.verify_existing(project_id, existing, staged)
            ref = existing
        else:
            ref = repo.objects.publish(project_id, staged)
        with repo.database.transaction() as tx:
            # A withdrawal during staging, HEAD, legacy verification or upload must take effect.
            repo._acl(tx, project_id, write=True)
            check_identity(repo, tx, project_id, staged.sha256)
            if existing is None:
                query(
                    tx,
                    "INSERT INTO blobs VALUES(:p,:key,:sha,:n,:media) ON CONFLICT DO NOTHING",
                    p=project_id,
                    key=ref.storage_key,
                    sha=ref.sha256,
                    n=ref.byte_length,
                    media=ref.media_type,
                )
            winner = lookup(tx, project_id, staged)
            if winner is None:
                raise ContractError("blob_publication_missing")
        if winner != ref:
            # A legacy publisher may have won with a historical random key. Verify that handle,
            # outside the transaction, then check authorization again before returning it.
            repo.objects.verify_existing(project_id, winner, staged)
            with repo.database.transaction() as tx:
                repo._acl(tx, project_id, write=True)
                repo._check_blob(tx, project_id, winner)
        return winner
