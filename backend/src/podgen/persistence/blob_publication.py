"""Publish verified bytes only after fresh authorization; no network inside SQL transactions."""

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError

from .batch_insert import insert_rows
from .blob_access import validate_identity
from .database import json_text, one, query


def check_identity(repo, tx, project_id, sha256, *, public_only=False):
    from .blob_access import validate_identity

    facts = one(
        tx,
        """SELECT
        EXISTS(SELECT 1 FROM protected_byte_hashes WHERE project_id=:p AND sha256=:sha) AS evaluator_only,
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


def _inspect_batch(repo, tx, project, staged):
    """One exact pre/post-I/O view: current ACL, SHA flags and existing bindings."""
    identities = list({(item.sha256, item.media_type) for item in staged})
    found = {}
    for row in query(
        tx,
        """SELECT wanted.sha256,wanted.media_type AS requested_media_type,m.role,
        b.storage_key,b.byte_length,b.media_type,
        EXISTS(SELECT 1 FROM protected_byte_hashes c WHERE c.project_id=:p AND c.sha256=wanted.sha256) AS evaluator_only,
        EXISTS(SELECT 1 FROM blob_access_revocations v WHERE v.project_id=:p AND v.sha256=wanted.sha256) AS access_revoked
        FROM jsonb_to_recordset(CAST(:identities AS jsonb)) AS wanted(sha256 text,media_type text)
        LEFT JOIN project_members m ON m.project_id=:p AND m.actor_id=:actor
        LEFT JOIN blobs b ON b.project_id=:p AND b.sha256=wanted.sha256 AND b.media_type=wanted.media_type""",
        p=project,
        actor=repo.actor_id,
        identities=json_text([dict(sha256=sha, media_type=media) for sha, media in identities]),
    ).mappings():
        role = repo._require_role(row["role"], write=True)
        validate_identity(
            evaluator_only=row["evaluator_only"],
            access_revoked=row["access_revoked"],
            purpose=repo.purpose,
            role=role,
        )
        if row["storage_key"] is not None:
            found[row["sha256"], row["requested_media_type"]] = BlobRef(
                **{key: row[key] for key in ("storage_key", "sha256", "byte_length", "media_type")}
            )
    return found


def publish_staged(repo, project, staged, *, publish=None):
    """One storage policy; optionally commit a receipt with its blob bindings.

    The callback is SQL-only. A legacy-key winner is verified outside the rolled
    back transaction before either its binding or receipt can be returned.
    """
    from .archive_authorization import archive_write

    if not staged or len(staged) > 64:
        raise ContractError("invalid_blob_publication_size")
    with repo.database.transaction() as tx:
        existing = _inspect_batch(repo, tx, project, staged)
    published = {}
    for item in staged:
        key = item.sha256, item.media_type
        if key in existing:
            item.matches(existing[key])
            repo.objects.verify_existing(project, existing[key], item)
            published[key] = existing[key]
        else:
            published[key] = repo.objects.publish(project, item)

    def commit(tx):
        current = _inspect_batch(repo, tx, project, staged)
        new = {key: ref for key, ref in published.items() if key not in current}
        inserted = (
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
                    for ref in new.values()
                ],
                on_conflict=True,
                returning=True,
            )
            or []
        )
        winners = current | {
            (row["sha256"], row["media_type"]): BlobRef(
                **{key: row[key] for key in ("storage_key", "sha256", "byte_length", "media_type")}
            )
            for row in inserted
        }
        missing = [item for item in staged if (item.sha256, item.media_type) not in winners]
        if missing:
            # READ COMMITTED scalar writers can lose INSERT to a row committed
            # after the first inspection. This fresh, race-only lookup checks the
            # winner and current flags; RR receipt writers retry 40001 instead.
            winners.update(_inspect_batch(repo, tx, project, missing))
        if any((item.sha256, item.media_type) not in winners for item in staged):
            raise ContractError("blob_publication_missing")
        changed = []
        for item in staged:
            winner = winners[item.sha256, item.media_type]
            item.matches(winner)
            if winner != published[item.sha256, item.media_type]:
                changed.append((item, winner))
        if changed:
            raise _UnverifiedWinners(changed)
        refs = [winners[item.sha256, item.media_type] for item in staged]
        return publish(tx, refs) if publish is not None else refs

    for attempt in range(3):
        try:
            if publish is not None:
                return archive_write(repo, project, commit)
            with repo.database.transaction() as tx:
                return commit(tx)
        except _UnverifiedWinners as error:
            for item, winner in error.changed:
                repo.objects.verify_existing(project, winner, item)
                published[item.sha256, item.media_type] = winner
            if attempt == 2:
                raise ContractError("blob_publication_unstable") from None


class _UnverifiedWinners(Exception):
    def __init__(self, changed):
        self.changed = changed
