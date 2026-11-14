"""Live run/input/entry access. Immutable locators never grant byte access."""

from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one
from podgen.persistence.execution.live import authorize
from podgen.persistence.read_authorization import checked_node


def run_access(repo, tx, project, run_id, *, write=False, adopt=False):
    role = repo._acl(tx, project, write=write)
    row = checked_node(
        repo,
        tx,
        ("private_run_owner", project, run_id),
        lambda: one(
            tx,
            "SELECT actor_id,execution_storage FROM evaluation_writing_runs WHERE project_id=:p AND run_id=:r",
            p=project,
            r=run_id,
        ),
    )
    if row is None or repo.purpose != "evaluation" or role not in ("owner", "evaluator", "operator"):
        raise AccessDenied()
    if write and (
        row["actor_id"] != repo.actor_id
        or (row["execution_storage"] != "private/3" and not (adopt and row["execution_storage"] == "legacy"))
    ):
        raise AccessDenied()
    return row


def inputs(repo, tx, project, run_id, set_id, *, action=False, write=False, adopt=False):
    run_access(repo, tx, project, run_id, write=write, adopt=adopt)
    owner = checked_node(
        repo,
        tx,
        ("private_input_owner", project, run_id, set_id),
        lambda: one(
            tx,
            "SELECT 1 FROM execution_input_sets WHERE project_id=:p AND run_id=:r AND set_id=:s",
            p=project,
            r=run_id,
            s=set_id,
        ),
    )
    if owner is None:
        raise AccessDenied()
    authorize(repo, tx, project, set_id, action=action)


def private_hashes(tx, project, hashes):
    if hashes and one(
        tx,
        "SELECT 1 FROM blob_access_revocations WHERE project_id=:p AND sha256=ANY(CAST(:hashes AS text[])) LIMIT 1",
        p=project,
        hashes=list(hashes),
    ):
        raise AccessDenied()


def entry_privacy(tx, project, entry_ids):
    if not entry_ids:
        return
    denied = one(
        tx,
        """SELECT EXISTS(SELECT 1 FROM execution_entry_denials d
        WHERE d.project_id=:p AND d.entry_id=ANY(CAST(:ids AS uuid[]))) OR EXISTS(
        SELECT 1 FROM execution_entries e JOIN blob_access_revocations v
          ON v.project_id=e.project_id AND e.owned_hashes @> ARRAY[v.sha256]
        WHERE e.project_id=:p AND e.entry_id=ANY(CAST(:ids AS uuid[]))) AS denied""",
        p=project,
        ids=list(entry_ids),
    )
    if denied["denied"]:
        raise AccessDenied()


def conditions(tx, project, parents, owned_hashes):
    """One live privacy check over already loaded immutable parent descriptors."""
    hashes = set(owned_hashes) | {sha for row in parents.values() for sha in row["owned_hashes"]}
    if not parents and not hashes:
        return
    row = one(
        tx,
        """SELECT EXISTS(SELECT 1 FROM execution_entry_denials
        WHERE project_id=:p AND entry_id=ANY(CAST(:entries AS uuid[]))) OR
        EXISTS(SELECT 1 FROM blob_access_revocations
        WHERE project_id=:p AND sha256=ANY(CAST(:hashes AS text[]))) AS denied""",
        p=project,
        entries=list(parents),
        hashes=list(hashes),
    )
    if row["denied"]:
        raise AccessDenied()
