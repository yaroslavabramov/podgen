"""Structural commands are checked before any object upload or database mutation."""

from uuid import UUID

from podgen.domain.errors import ContractError

from .contracts import Checkpoint, HeadChange, PrivateBytes, PrivateObjectUse

KINDS = frozenset(
    {
        "attempt_prepared",
        "count_intent",
        "count_error",
        "count_receipt",
        "count_receipt_submit_intent",
        "count_receipt_count_intent",
        "submit_intent",
        "provider_receipt",
        "tool_intent",
        "tool_result",
        "attempt_output",
        "attempt_failed",
        "accounting_correction",
        "authorized_replacement",
        "nested_begin",
        "nested_return",
        "legacy_adopted",
        "observation",
        "semantic_published",
        "structured_decision",
        "legacy_receipt",
        "attempt_response",
    }
)
PHASES = frozenset(
    {
        "prepared",
        "counting",
        "counted",
        "submitting",
        "waiting",
        "response_completed",
        "tool_running",
        "completed",
        "failed",
        "validation_failed",
        "outcome_unknown",
    }
)
CLEARABLE = frozenset(
    {
        "request_entry_id",
        "provider_entry_id",
        "submit_entry_id",
        "pending_parent_entry_id",
        "request_hash",
        "configuration_hash",
        "count_identity",
        "count_entry_id",
        "provider_id",
    }
)


def validate(command):
    if not isinstance(command, Checkpoint) or not isinstance(command.head, HeadChange):
        raise TypeError("Checkpoint command required")
    if not isinstance(command.data, dict):
        raise ContractError("invalid_private_checkpoint_data")
    if command.kind not in KINDS or command.head.phase is not None and command.head.phase not in PHASES:
        raise ContractError("invalid_private_checkpoint_kind_or_phase")
    if (
        any(
            not isinstance(value, UUID)
            for value in (
                command.run_id,
                command.attempt_id,
                command.operation_id,
                command.input_set_id,
                *command.parents,
            )
        )
        or command.step_id is not None
        and not isinstance(command.step_id, UUID)
    ):
        raise ContractError("invalid_private_checkpoint_identity")
    if (
        any(
            type(value) is not int or not 0 <= value < 2**63
            for value in (
                command.expected_revision,
                command.observation_no,
            )
        )
        or command.step_index is not None
        and (type(command.step_index) is not int or not 0 <= command.step_index < 2**31)
    ):
        raise ContractError("invalid_private_checkpoint_revision_or_index")
    if command.step_id is None and command.step_index is not None:
        raise ContractError("private_step_index_without_step")
    if len(command.objects) > 64 or len(command.views) > 128 or len(command.parents) > 64:
        raise ContractError("private_checkpoint_batch_too_large")
    if len(set(command.parents)) != len(command.parents):
        raise ContractError("private_duplicate_parent")
    if any(not isinstance(value, (PrivateBytes, PrivateObjectUse)) for value in command.objects.values()):
        raise ContractError("private_checkpoint_unverified_objects")
    if set(command.head.clear_fields) - CLEARABLE or any(
        getattr(command.head, field) is not None for field in command.head.clear_fields
    ):
        raise ContractError("invalid_private_head_clear")
    if command.head.preserve_active and (
        command.kind
        not in (
            "accounting_correction",
            "legacy_receipt",
            "attempt_response",
            "provider_receipt",
            "observation",
        )
        or command.head.phase is not None
        or command.head.clear_fields
        or any(getattr(command.head, field) is not None for field in CLEARABLE)
    ):
        raise ContractError("private_reconciliation_cannot_change_coordinator")
    for view in command.views:
        if set(view) - {"projection"} != {"record_id", "record_kind", "payload"} or view.get(
            "projection", "identity/3"
        ) not in {"identity/3", "physical-request/3"}:
            raise ContractError("invalid_private_archive_view")
        from podgen.domain.contracts.common import RuntimeRef

        RuntimeRef(record_id=view["record_id"], record_kind=view["record_kind"])
