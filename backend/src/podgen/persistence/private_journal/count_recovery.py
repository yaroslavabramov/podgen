"""Bind count retries to a definitive saved rejection of the exact candidate."""

from uuid import UUID

from podgen.domain.errors import ContractError

from .contracts import operation_id


def _owner(rows, reference, kind):
    identity = reference.get("record_id") if isinstance(reference, dict) else None
    matches = [
        row
        for row in rows.values()
        if any(
            view["record_id"] == identity and view.get("event", {}).get("kind") == kind
            for view in row["archive_views"]
        )
    ]
    if len(matches) != 1:
        raise ContractError("private_count_recovery_evidence_missing")
    return matches[0]


def validate(command, previous, rows):
    if command.kind == "count_error":
        intent = _owner(rows, command.data.get("count_intent_ref"), "source_context_count_intent")
        errors = [
            view["payload"]
            for view in command.views
            if view["payload"].get("event_kind") == "source_context_count_error"
        ]
        if len(errors) != 1:
            raise ContractError("private_count_recovery_evidence_missing")
        error = errors[0]
        status = error.get("provider_http_status")
        if (
            not previous
            or previous["phase"] != "counting"
            or previous["active_attempt_id"] != intent["attempt_id"]
            or previous["active_step_id"] != intent["step_id"]
            or intent["checkpoint_kind"] not in ("count_intent", "count_receipt_count_intent")
            or intent["operation_id"] != command.operation_id
            or intent["step_id"] != command.step_id
            or intent["attempt_id"] != command.attempt_id
            or error["data"].get("ambiguous") is not False
            or error["data"].get("retryable") is not True
            or type(status) is not int
            or not (status == 408 or 500 <= status <= 599)
            or command.data.get("count_identity") != previous["count_identity"]
        ):
            raise ContractError("private_count_recovery_evidence_mismatch")
    reference = command.data.get("retry_error_ref")
    if reference is None:
        return
    error = _owner(rows, reference, "source_context_count_error")
    expected = operation_id(
        command.step_id, "count", command.data.get("count_identity"), str(UUID(reference["record_id"]))
    )
    if (
        command.kind != "count_intent"
        or not previous
        or previous["phase"] != "prepared"
        or error["entry_id"] != previous["last_entry_id"]
        or error["checkpoint_kind"] != "count_error"
        or error["step_id"] != command.step_id
        or error["attempt_id"] != command.attempt_id
        or error["data"].get("count_identity") != command.data.get("count_identity")
        or command.operation_id != expected
    ):
        raise ContractError("private_count_retry_not_bound")
