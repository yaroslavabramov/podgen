"""Narrow evaluation archive transaction: one permission snapshot and no repeated ancestor walks."""

from sqlalchemy.exc import DBAPIError

from .database import query
from .read_authorization import authorized_read


def archive_write(repo, project, write):
    """Publish after one full check; project/member locks serialize supported withdrawals.

    This is not the general transaction policy. No provider/network operations belong
    in `write`. A serialization retry repeats only rolled-back immutable SQL publication.
    """
    for attempt in range(3):
        try:
            with repo.database.engine.begin() as tx:
                tx.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                # DependencyLedger withdraws under the project's FOR UPDATE lock. If a
                # withdrawal won before this snapshot acquired its lock, PostgreSQL
                # raises 40001; retry takes a fresh snapshot and sees the withdrawal.
                query(tx, "SELECT project_id FROM projects WHERE project_id=:p FOR SHARE", p=project)
                # Also serialize direct ACL deletion/demotion, without relying on callers
                # of project_members to acquire the project's lock themselves.
                query(
                    tx,
                    "SELECT role FROM project_members WHERE project_id=:p AND actor_id=:a FOR SHARE",
                    p=project,
                    a=repo.actor_id,
                )
                with authorized_read(repo, tx):
                    return write(tx)
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                raise
