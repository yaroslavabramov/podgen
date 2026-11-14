"""Bounded privacy-only checks; a parent binding never confers read access."""

from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one
from podgen.persistence.read_authorization import checked_node


def authorize_parents(repo, tx, project, records, *, include_parents=True):
    records = tuple(sorted(set(records), key=str))
    state = repo._read_authorization.get()
    active = (
        state is not None
        and state.transaction is tx
        and state.actor == repo.actor_id
        and state.purpose == repo.purpose
    )

    def key(identity):
        return ("execution_parent_privacy", project, identity, include_parents)

    pending = [identity for identity in records if not active or key(identity) not in state.completed]
    for offset in range(0, len(pending), 64):
        batch = pending[offset : offset + 64]
        denied = one(
            tx,
            """WITH nodes(record_id) AS (
            SELECT unnest(CAST(:records AS uuid[]))
            UNION SELECT target_record_id FROM runtime_runtime_refs
             WHERE :include AND project_id=:p AND record_id=ANY(CAST(:records AS uuid[]))
          ) SELECT EXISTS(SELECT 1 FROM nodes n JOIN execution_record_withdrawals w
               ON w.project_id=:p AND w.record_id=n.record_id)
            OR EXISTS(SELECT 1 FROM nodes n JOIN runtime_blob_refs b
               ON b.project_id=:p AND b.record_id=n.record_id
             JOIN blob_access_revocations v ON v.project_id=b.project_id AND v.sha256=b.sha256) AS denied""",
            p=project,
            records=batch,
            include=include_parents,
        )
        if denied["denied"]:
            raise AccessDenied()
        for identity in batch:
            checked_node(repo, tx, key(identity), lambda: True)
