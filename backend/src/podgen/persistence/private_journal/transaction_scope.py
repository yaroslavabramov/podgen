"""One run/input/head metadata boundary, scoped to its actual SQL transaction."""

from podgen.domain.errors import AccessDenied
from podgen.persistence.access_facts import access_facts
from podgen.persistence.database import one


def load(repo, tx, project, command, *, lock):
    membership = "" if lock else " JOIN project_members m ON m.project_id=r.project_id AND m.actor_id=:actor"
    role_column = "" if lock else ",m.role AS member_role"
    row = one(
        tx,
        """SELECT h.*,r.actor_id AS owner_actor_id,r.execution_storage AS run_storage,
        s.set_id AS owned_input_set"""
        + role_column
        + """
        FROM evaluation_writing_runs r
        JOIN execution_input_sets s ON s.project_id=r.project_id AND s.run_id=r.run_id AND s.set_id=:set_id
        LEFT JOIN execution_heads h ON h.project_id=r.project_id AND h.run_id=r.run_id
        """
        + membership
        + " WHERE r.project_id=:p AND r.run_id=:r"
        + (" FOR UPDATE OF r" if lock else ""),
        p=project,
        r=command.run_id,
        set_id=command.input_set_id,
        actor=repo.actor_id,
    )
    if row is None:
        raise AccessDenied()
    facts = access_facts(repo, tx)
    if not lock:
        facts.roles[project] = dict(role=row["member_role"])
    role = repo._acl(tx, project, write=True)
    if (
        repo.purpose != "evaluation"
        or role not in ("owner", "evaluator", "operator")
        or row["owner_actor_id"] != repo.actor_id
    ):
        raise AccessDenied()
    if row["run_storage"] != "private/3" and not (
        command.kind == "legacy_adopted" and row["run_storage"] == "legacy"
    ):
        raise AccessDenied()
    state = repo._read_authorization.get()
    state.completed["private_run_owner", project, command.run_id] = dict(
        actor_id=row["owner_actor_id"], execution_storage=row["run_storage"]
    )
    state.completed["private_input_owner", project, command.run_id, command.input_set_id] = True
    return (
        {
            key: value
            for key, value in row.items()
            if key
            not in {
                "owner_actor_id",
                "run_storage",
                "owned_input_set",
                "member_role",
            }
        }
        if row["revision"] is not None
        else None
    )
