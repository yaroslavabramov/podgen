"""Private control proof referring to an actual immutable legacy receipt, without copying it."""

import json
from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one
from podgen.persistence.private_journal import access
from podgen.persistence.private_journal.contracts import Checkpoint, HeadChange, operation_id
from podgen.persistence.private_journal.head import next_head
from podgen.persistence.private_journal.writer import commit
from podgen.persistence.read_authorization import _read_connection


def adopt(journal, root, ref, record, raw, calls):
    physical = UUID(record["data"].get("source_turn_id", str(root["invocation_id"])))
    index = record["data"].get("source_turn_index")
    operation = operation_id(physical, "legacy_receipt", str(ref.record_id))
    with _read_connection(journal.repo) as tx:
        found = one(
            tx,
            """SELECT entry_id,input_set_id FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND operation_id=:op AND checkpoint_kind='legacy_receipt'""",
            p=journal.project,
            r=root["run_id"],
            op=operation,
        )
        if found:
            access.inputs(journal.repo, tx, journal.project, root["run_id"], found["input_set_id"])
            access.entry_privacy(tx, journal.project, [found["entry_id"]])
            return found["entry_id"]
    response = json.loads(raw)
    if response.get("status") != "completed":
        raise Conflict("legacy_provider_response_status_unproven")
    references = [ref, RuntimeRef.model_validate(record["request_ref"])]
    if record["data"].get("turn_request_ref"):
        references.append(RuntimeRef.model_validate(record["data"]["turn_request_ref"]))
    bound = journal.execution.prepare(root["run_id"], evaluation=references)
    previous = journal.execution.current_head(root["run_id"])
    if previous is None:
        from podgen.persistence.private_journal.state import turn

        from .legacy_adoption import adopt as adopt_run

        target = turn(journal, root, index) if index is not None else root
        previous = adopt_run(journal, root, dict(root, **target))
    normalized = [
        {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()} for row in calls
    ]
    command = Checkpoint(
        run_id=root["run_id"],
        attempt_id=root["invocation_id"],
        operation_id=operation,
        kind="legacy_receipt",
        input_set_id=bound["set_id"],
        expected_revision=previous["revision"],
        step_id=physical,
        step_index=index,
        data=dict(
            legacy_receipt_ref=ref.model_dump(mode="json"),
            provider_calls=normalized,
            state=dict(status="response_completed"),
        ),
        head=HeadChange(preserve_active=True),
    )
    result = commit(journal.repo, journal.project, command)
    if result["created"]:
        journal.execution.committed(root["run_id"], next_head(previous, command))
    return command.entry_id
