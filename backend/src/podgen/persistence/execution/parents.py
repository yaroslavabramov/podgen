"""Bounded privacy-only checks; a parent binding never confers read access."""

from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one
from podgen.persistence.read_authorization import checked_node


def authorize_parents(repo, tx, project, records, *, include_parents=True):
    records = tuple(sorted(set(records), key=str))
    if not records:
        return

    def check():
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
            records=list(records),
            include=include_parents,
        )
        if denied["denied"]:
            raise AccessDenied()
        return True

    return checked_node(repo, tx, ("execution_parent_privacy", project, records, include_parents), check)
