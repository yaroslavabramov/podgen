"""A fixed current coordinator and accounting vector; history belongs to entries."""

from podgen.domain.errors import Conflict, ContractError

from .contracts import ACCOUNTING_FIELDS

UNKNOWN = {"submitting", "waiting", "outcome_unknown"}
RECOVERY_KINDS = {"authorized_replacement", "nested_begin", "nested_return", "legacy_adopted"}


def next_head(previous, command):
    previous = dict(previous or {})
    change = command.head
    effective_phase = change.phase or previous.get("phase") or "prepared"
    if command.kind == "provider_receipt" and change.preserve_active:
        # An archived outcome does not transition the active child's phase.
        effective_phase = None
    expected = {
        "attempt_prepared": {"prepared"},
        "count_intent": {"counting"},
        "count_error": {"prepared"},
        "count_receipt": {"counted", "failed", "validation_failed"},
        "count_receipt_submit_intent": {"submitting"},
        "count_receipt_count_intent": {"counting"},
        "submit_intent": {"submitting"},
        "provider_receipt": {
            "waiting",
            "response_completed",
            "failed",
            "validation_failed",
            "outcome_unknown",
            "submitting",
        },
        "tool_intent": {"tool_running", "response_completed"},
        "tool_result": {"tool_running", "response_completed"},
        "attempt_output": {"completed"},
        "attempt_failed": {"failed", "validation_failed", "outcome_unknown"},
        "authorized_replacement": {"prepared"},
        "nested_begin": {"prepared"},
        "semantic_published": {"completed"},
    }.get(command.kind)
    if expected is not None and effective_phase is not None and effective_phase not in expected:
        raise ContractError("private_checkpoint_phase_mismatch")
    if command.kind == "observation" and previous and effective_phase != previous["phase"]:
        raise ContractError("private_observation_cannot_change_phase")
    switched = (
        previous.get("active_step_id") != command.step_id
        or previous.get("active_attempt_id") != command.attempt_id
    )
    if (
        previous.get("phase") == "completed"
        and not switched
        and not change.preserve_active
        and command.kind
        not in (
            "semantic_published",
            "observation",
            "nested_return",
        )
    ):
        raise Conflict("private_completed_operation_is_terminal")
    if (
        switched
        and not change.preserve_active
        and previous.get("phase") in UNKNOWN
        and command.kind not in RECOVERY_KINDS
    ):
        raise Conflict("private_unknown_operation_requires_authorized_recovery")
    if command.kind == "authorized_replacement" and not command.data.get("authorization"):
        raise ContractError("private_replacement_authorization_missing")
    if (
        not change.preserve_active
        and previous.get("phase") in UNKNOWN
        and (
            change.phase in ("prepared", "counting", "counted", "submitting")
            and command.kind
            in {
                "attempt_prepared",
                "count_intent",
                "count_receipt",
                "count_receipt_submit_intent",
                "count_receipt_count_intent",
                "submit_intent",
            }
        )
    ):
        raise Conflict("private_unknown_operation_requires_authorized_recovery")
    if previous.get("phase") == "counting" and command.kind == "count_intent":
        raise Conflict("private_unknown_count_requires_authorized_recovery")
    accounting = dict(previous.get("accounting") or {})
    for field, delta in change.accounting_delta.items():
        if field not in ACCOUNTING_FIELDS or type(delta) is not int:
            raise ContractError("invalid_private_accounting_delta")
        value = accounting.get(field, 0) + delta
        if not 0 <= value <= 2**63 - 1:
            raise ContractError("private_accounting_overflow")
        accounting[field] = value
    head = {
        field: previous.get(field)
        for field in (
            "active_attempt_id",
            "active_step_id",
            "step_index",
            "phase",
            "request_entry_id",
            "provider_entry_id",
            "submit_entry_id",
            "pending_parent_entry_id",
            "request_hash",
            "configuration_hash",
            "count_identity",
            "count_entry_id",
            "provider_id",
            "observation_no",
        )
    }
    if not change.preserve_active:
        if switched:
            for field in (
                "request_entry_id",
                "provider_entry_id",
                "submit_entry_id",
                "request_hash",
                "configuration_hash",
                "count_identity",
                "count_entry_id",
                "provider_id",
                "pending_parent_entry_id",
            ):
                head[field] = None
        head.update(
            active_attempt_id=command.attempt_id,
            active_step_id=command.step_id,
            step_index=command.step_index,
            phase=change.phase or previous.get("phase") or "prepared",
            observation_no=(
                command.observation_no
                if switched or command.kind == "provider_receipt"
                else previous.get("observation_no", 0)
            ),
        )
        for field in (
            "request_entry_id",
            "provider_entry_id",
            "submit_entry_id",
            "pending_parent_entry_id",
            "request_hash",
            "configuration_hash",
            "count_identity",
            "count_entry_id",
            "provider_id",
        ):
            value = getattr(change, field)
            if value is not None:
                head[field] = value
    elif not previous:
        raise ContractError("private_reconciliation_without_head")
    for field in change.clear_fields:
        head[field] = None
    return dict(
        head,
        revision=command.expected_revision + 1,
        last_entry_id=command.entry_id,
        input_set_id=previous.get("input_set_id") if change.preserve_active else command.input_set_id,
        accounting=accounting,
    )
