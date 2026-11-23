"""Derive bounded operation charges from captured facts, separately from head status."""

from dataclasses import replace
from uuid import UUID

from podgen.persistence.private_journal.accounting import effective
from podgen.persistence.private_journal.contracts import ChargeChange, operation_id
from podgen.persistence.read_authorization import _read_connection


def compile_charges(phase, command, by_kind, intent, receipt):
    owner = phase.journal.execution
    proposed = {}
    if intent:
        proposed[command.operation_id] = dict(count_calls=1, uncertain_calls=1)
    if receipt:
        count_identity = receipt["data"].get("count_identity", (phase.previous or {}).get("count_identity"))
        count_operation = operation_id(command.step_id, "count", count_identity)
        if receipt["data"].get("count_operation_id"):
            count_operation = UUID(receipt["data"]["count_operation_id"])
        proposed[count_operation] = dict(count_calls=1)
    if command.kind == "count_error":
        proposed[command.operation_id] = dict(count_calls=1)
    reused = by_kind.get("count_receipt_reused")
    if reused:
        proposed[operation_id(command.step_id, "count_reuse", reused["data"]["count_identity"])] = dict(
            count_reuses=1
        )
    submit = by_kind.get("submit_intent")
    submit_operation = operation_id(command.step_id, "submit") if command.step_id else None
    if submit:
        proposed[submit_operation] = dict(
            generation_calls=1,
            input_tokens=phase.state[1]["input_tokens"],
            output_reserved=submit["data"]["output_reserved"],
            uncertain_calls=1,
        )
        if getattr(phase, "cost_reserved_microusd", None):
            proposed[submit_operation]["cost_microusd"] = phase.cost_reserved_microusd
    usage = command.data.get("receipt_state", command.data.get("state", {}))
    if command.kind == "provider_receipt" and usage.get("status") == "response_completed":
        if usage.get("output_tokens") is not None:
            proposed[submit_operation] = dict(
                generation_calls=1,
                input_tokens=usage.get("input_tokens", phase.invocation.get("input_tokens")) or 0,
                output_tokens=usage["output_tokens"],
                cached_input_tokens=getattr(phase, "cached_input_tokens", 0),
            )
            if getattr(phase, "cost_microusd", None) is not None:
                proposed[submit_operation]["cost_microusd"] = phase.cost_microusd
    if command.data.get("rejection"):
        proposed[submit_operation] = dict(generation_calls=1, rejected_calls=1)
    changes, delta = [], {}
    owner.charge_scope(command.run_id, command.step_id)
    for operation, charge in proposed.items():
        old_revision, old = (
            load(owner, operation) if command.head.preserve_active else owner.charge(operation)
        )
        if old == charge:
            continue
        changes.append(ChargeChange(operation, old_revision, old, charge, command.entry_id))
        for field in old.keys() | charge.keys():
            delta[field] = delta.get(field, 0) + charge.get(field, 0) - old.get(field, 0)
    delta = {key: value for key, value in delta.items() if value}
    # Optional usage interpretation must never discard an already captured receipt.
    # Keep conservative prior exposure if a valid individual value overflows totals.
    totals = (phase.previous or {}).get("accounting", {})
    if command.kind == "provider_receipt" and any(
        not 0 <= totals.get(key, 0) + value < 2**63 for key, value in delta.items()
    ):
        data = dict(command.data, accounting_error="aggregate_token_usage_overflow")
        if command.head.preserve_active:
            data.update(state={}, receipt_state=dict(usage, status="validation_failed"))
            return replace(command, data=data)
        data["state"] = dict(command.data["state"], status="validation_failed")
        return replace(command, data=data, head=replace(command.head, phase="validation_failed"))
    return replace(command, charges=tuple(changes), head=replace(command.head, accounting_delta=delta))


def load(owner, operation):
    with _read_connection(owner.repo) as tx:
        return effective(tx, owner.project, owner._charge_run, operation)
