"""One typed atomic checkpoint writer. No generic runtime/event/blob publications."""

from copy import deepcopy

from sqlalchemy.exc import IntegrityError

from podgen.domain.errors import Conflict, ContractError
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.database import json_text, one, query
from podgen.persistence.execution.inputs import _publish
from podgen.persistence.execution.reads import current_inputs
from podgen.persistence.read_authorization import authorized_read

from . import access, bindings, control, transaction_scope
from .accounting import changes
from .contracts import PrivateBytes, PrivateObjectUse
from .encoding import JSON_COLUMNS, encode, entry_row
from .head import next_head
from .objects import register, upload
from .validation import validate


def _existing(tx, project, command, fingerprint):
    row = one(
        tx,
        "SELECT entry_id,content_hash,revision FROM execution_entries WHERE project_id=:p AND run_id=:r AND commit_key=:key",
        p=project,
        r=command.run_id,
        key=command.commit_key,
    )
    if row is not None and row["content_hash"] != fingerprint:
        raise Conflict("private_checkpoint_reused_with_different_content")
    return dict(row, created=False) if row is not None else None


def commit(repo, project, command):
    validate(command)
    command = deepcopy(command)
    body, fingerprint = encode(command)
    pending_bytes = list(
        {obj.reference: obj for obj in command.objects.values() if isinstance(obj, PrivateBytes)}.values()
    )
    prepared_parents = None
    if pending_bytes:
        existing, prepared_parents = preflight(repo, project, command, fingerprint)
        if existing is not None:
            return existing
        upload(repo, project, pending_bytes)

    def write(tx):
        previous = transaction_scope.load(repo, tx, project, command, lock=True)
        if (previous["revision"] if previous else 0) != command.expected_revision:
            existing = _existing(tx, project, command, fingerprint)
            if existing is not None:
                authorize_command(repo, tx, project, command, parent_rows=prepared_parents)
                return existing
            raise Conflict("private_checkpoint_revision_conflict")
        parent_rows = (
            prepared_parents
            if prepared_parents is not None
            else control.parents(repo, tx, project, command, authorize=False)
        )
        control.validate(repo, tx, project, command, previous, parent_rows)
        charge_facts = changes(tx, project, command)
        head = next_head(previous, command)
        objects = list({obj.reference for obj in command.objects.values()})
        fresh_hashes = register(tx, project, command.entry_id, objects)
        row = entry_row(project, command, head, fingerprint, body, charge_facts)
        aliases = row["archive_ids"]
        if aliases and one(
            tx,
            """SELECT 1 FROM runtime_records WHERE project_id=:p AND record_id=ANY(CAST(:ids AS uuid[]))
            UNION ALL SELECT 1 FROM execution_entries WHERE project_id=:p AND archive_ids && CAST(:ids AS uuid[]) LIMIT 1""",
            p=project,
            ids=aliases,
        ):
            raise Conflict("private_archive_reference_already_owned")
        insert_rows(tx, "execution_entries", [row], json_columns=JSON_COLUMNS)
        if fresh_hashes:
            from .privacy import protect_private_owned

            protect_private_owned(tx, project, command.entry_id, fresh_hashes)
        # This is the first input/graph authorization inside this transaction;
        # private classification is already visible, with no stale prewrite facts.
        authorize_command(repo, tx, project, command, parent_rows=parent_rows)
        if command.semantic_result is not None:
            from .semantic import publish

            publish(repo, tx, project, command)
        if command.kind == "legacy_adopted":
            query(
                tx,
                "UPDATE evaluation_writing_runs SET execution_storage='private/3' WHERE project_id=:p AND run_id=:r AND execution_storage='legacy'",
                p=project,
                r=command.run_id,
            )
        fields = dict(head, project_id=project, run_id=command.run_id)
        fields["accounting"] = json_text(fields["accounting"])
        if previous is None:
            insert_rows(tx, "execution_heads", [fields], json_columns=("accounting",))
        else:
            assignments = ",".join(
                f"{key}=CAST(:{key} AS jsonb)" if key == "accounting" else f"{key}=:{key}" for key in head
            )
            updated = query(
                tx,
                f"UPDATE execution_heads SET {assignments} WHERE project_id=:project_id AND run_id=:run_id AND revision=:expected RETURNING revision",
                **fields,
                expected=command.expected_revision,
            ).scalar()
            if updated is None:
                raise Conflict("private_checkpoint_revision_conflict")
        return dict(
            entry_id=command.entry_id, content_hash=fingerprint, revision=head["revision"], created=True
        )

    try:
        return _publish(repo, project, write)
    except IntegrityError as error:
        if (
            getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            != "execution_entries_project_id_run_id_commit_key_key"
        ):
            raise
        with repo.database.read_transaction() as tx, authorized_read(repo, tx):
            transaction_scope.load(repo, tx, project, command, lock=False)
            authorize_command(repo, tx, project, command)
            existing = _existing(tx, project, command, fingerprint)
            if existing is None:
                raise
            return existing


def authorize_command(repo, tx, project, command, *, parent_rows=None):
    access.inputs(
        repo,
        tx,
        project,
        command.run_id,
        command.input_set_id,
        action=command.action,
        write=True,
        adopt=command.kind == "legacy_adopted",
    )
    if parent_rows is None:
        parent_rows = control.parents(repo, tx, project, command, authorize=False)
    control.authorize_parents(repo, tx, project, command, parent_rows, privacy=False)
    bindings.validate(tx, project, command, parent_rows)
    for obj in command.objects.values():
        if isinstance(obj, PrivateObjectUse):
            parent = parent_rows.get(obj.owner_entry_id)
            if (
                parent is None
                or obj.reference.model_dump(mode="json") not in parent["owned_objects"].values()
            ):
                raise ContractError("private_object_use_owner_mismatch")
    access.conditions(
        tx, project, parent_rows, {value.reference.sha256 for value in command.objects.values()}
    )
    return parent_rows


@current_inputs
def preflight(repo, project, command, fingerprint):
    # No authorization snapshot survives the external object upload.
    with repo.database.read_transaction() as tx, authorized_read(repo, tx):
        previous = transaction_scope.load(repo, tx, project, command, lock=False)
        parents = authorize_command(repo, tx, project, command)
        if (previous["revision"] if previous else 0) != command.expected_revision:
            existing = _existing(tx, project, command, fingerprint)
            if existing is None:
                raise Conflict("private_checkpoint_revision_conflict")
            return existing, parents
        return None, parents
