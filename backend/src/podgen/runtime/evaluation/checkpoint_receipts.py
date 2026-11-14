"""Bind each receipt to its own submit, independently of the active coordinator."""

from podgen.domain.errors import ContractError
from podgen.persistence.database import one
from podgen.persistence.private_journal import access
from podgen.persistence.read_authorization import _read_connection


def inactive_owner(phase, step_id):
    previous = phase.previous or {}
    return (
        previous.get("active_attempt_id") != phase.root["invocation_id"]
        or previous.get("active_step_id") != step_id
    )


def bind(phase, step_id, operation, data):
    late = inactive_owner(phase, step_id)
    if not late:
        previous = phase.previous or {}
        if previous.get("submit_entry_id") is None:
            raise ContractError("private_provider_receipt_without_submit")
        data["submit_entry_id"] = str(previous["submit_entry_id"])
        observation = previous["observation_no"] + 1 if previous.get("provider_entry_id") else 0
        return observation, False
    journal = phase.journal
    with _read_connection(journal.repo) as tx:
        access.run_access(journal.repo, tx, journal.project, phase.run_id)
        row = one(
            tx,
            """SELECT s.entry_id,COALESCE((
              SELECT max(e.observation_no)+1 FROM execution_entries e
              WHERE e.project_id=:p AND e.run_id=:r AND e.attempt_id=:attempt
                AND e.step_id=CAST(:step AS uuid) AND e.operation_id=:operation
                AND e.checkpoint_kind='provider_receipt'),0) AS observation_no
            FROM execution_entries s
            WHERE s.project_id=:p AND s.run_id=:r AND s.attempt_id=:attempt
              AND s.step_id=CAST(:step AS uuid) AND s.operation_id=:operation
              AND s.checkpoint_kind IN ('submit_intent','count_receipt_submit_intent')
            ORDER BY s.revision DESC LIMIT 1""",
            p=journal.project,
            r=phase.run_id,
            attempt=phase.root["invocation_id"],
            step=step_id,
            operation=operation,
        )
    if row is None:
        raise ContractError("private_provider_receipt_without_submit")
    data["submit_entry_id"] = str(row["entry_id"])
    # A retired parent's operational status is part of the replacement's
    # authorization. Settle its cost without reactivating it or rewriting
    # that evidence. The complete received outcome remains separately saved.
    received = data.pop("state", {})
    data["receipt_state"] = received
    data["state"] = {
        key: received[key]
        for key in ("input_tokens", "output_tokens")
        if received.get("status") == "response_completed" and key in received
    }
    return row["observation_no"], True
