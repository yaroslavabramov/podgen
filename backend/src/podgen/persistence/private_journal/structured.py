"""Idempotent metadata lookup for an authenticated structured provider decision."""

from podgen.domain.errors import Conflict
from podgen.persistence.database import one
from podgen.persistence.execution.reads import current_inputs
from podgen.persistence.read_authorization import _read_connection

from . import access


@current_inputs
def existing(repo, project, run_id, operation, provider_entry, calls):
    with _read_connection(repo) as tx:
        row = one(
            tx,
            """SELECT entry_id,input_set_id,data FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND operation_id=:op AND checkpoint_kind='structured_decision'""",
            p=project,
            r=run_id,
            op=operation,
        )
        if row is None:
            return None
        access.inputs(repo, tx, project, run_id, row["input_set_id"])
        access.entry_privacy(tx, project, [row["entry_id"]])
        if row["data"]["provider_entry_id"] != str(provider_entry) or row["data"]["provider_calls"] != calls:
            raise Conflict("structured_research_decision_binding_mismatch")
        return row["entry_id"]
