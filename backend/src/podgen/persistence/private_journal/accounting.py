"""Operation-local immutable charges; aggregate corrections never move the coordinator."""

from uuid import UUID

from podgen.domain.errors import Conflict, ContractError
from podgen.persistence.database import one

from .contracts import ACCOUNTING_FIELDS, ChargeChange


def vector(value):
    if not isinstance(value, dict) or any(
        key not in ACCOUNTING_FIELDS or type(amount) is not int or not 0 <= amount <= 2**63 - 1
        for key, amount in value.items()
    ):
        raise ContractError("invalid_private_charge")
    return {key: amount for key, amount in value.items() if amount}


def changes(tx, project, command):
    if len(command.charges) > 64:
        raise ContractError("private_charge_batch_too_large")
    facts, delta, seen = [], {}, set()
    for change in command.charges:
        if not isinstance(change, ChargeChange) or not isinstance(change.operation_id, UUID):
            raise ContractError("invalid_private_charge_change")
        if change.operation_id in seen:
            raise ContractError("duplicate_private_charge_operation")
        seen.add(change.operation_id)
        if type(change.expected_revision) is not int or change.expected_revision < 0:
            raise ContractError("invalid_private_charge_revision")
        if change.evidence_entry_id not in (*command.parents, command.entry_id):
            raise ContractError("private_charge_evidence_not_bound")
        old, new = vector(change.old), vector(change.new)
        row = one(
            tx,
            """SELECT charge_changes FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND charge_operations @> ARRAY[CAST(:op AS uuid)]
            ORDER BY revision DESC LIMIT 1""",
            p=project,
            r=command.run_id,
            op=change.operation_id,
        )
        prior = (
            next(
                (item for item in row["charge_changes"] if item["operation_id"] == str(change.operation_id)),
                None,
            )
            if row
            else None
        )
        if (prior["revision"] if prior else 0) != change.expected_revision or (
            vector(prior["new"]) if prior else {}
        ) != old:
            raise Conflict("private_charge_revision_conflict")
        for key in old.keys() | new.keys():
            delta[key] = delta.get(key, 0) + new.get(key, 0) - old.get(key, 0)
        facts.append(
            dict(
                operation_id=str(change.operation_id),
                revision=change.expected_revision + 1,
                old=old,
                new=new,
                evidence_entry_id=str(change.evidence_entry_id),
            )
        )
    delta = {key: value for key, value in delta.items() if value}
    supplied = command.head.accounting_delta
    if any(key not in ACCOUNTING_FIELDS or type(value) is not int for key, value in supplied.items()):
        raise ContractError("invalid_private_accounting_delta")
    if {key: value for key, value in supplied.items() if value} != delta:
        raise ContractError("private_accounting_delta_without_charge_fact")
    if command.kind == "accounting_correction" and (not facts or not command.head.preserve_active):
        raise ContractError("private_correction_must_preserve_coordinator")
    return facts


def effective(tx, project, run_id, operation):
    row = one(
        tx,
        """SELECT charge_changes FROM execution_entries
        WHERE project_id=:p AND run_id=:r AND charge_operations @> ARRAY[CAST(:op AS uuid)]
        ORDER BY revision DESC LIMIT 1""",
        p=project,
        r=run_id,
        op=operation,
    )
    if row:
        change = next(item for item in row["charge_changes"] if item["operation_id"] == str(operation))
        return change["revision"], change["new"]
    return 0, {}
