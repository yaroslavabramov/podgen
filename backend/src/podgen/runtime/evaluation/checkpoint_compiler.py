"""Compile the builder into one typed operation, not a list of SQL publications."""

from dataclasses import replace
from uuid import UUID, uuid5

from podgen.persistence.private_journal.contracts import (
    Checkpoint,
    HeadChange,
    operation_id,
)

from .checkpoint_dependencies import resolve
from .checkpoint_receipts import bind, inactive_owner


def compile_checkpoint(phase, *, action):
    journal, invocation, previous = phase.journal, phase.invocation, phase.previous or {}
    events = [view["payload"] for view in phase.views if "event_kind" in view["payload"]]
    by_kind = {event["event_kind"]: event for event in events}
    kinds = set(by_kind)
    step_id = invocation["invocation_id"]
    index = invocation.get("turn_index")
    if index is None:
        index = next(
            (
                event["data"].get("source_turn_index", event["data"].get("turn_index"))
                for event in reversed(events)
                if "source_turn_index" in event["data"] or "turn_index" in event["data"]
            ),
            None,
        )
        if index is not None:
            step_id = uuid5(phase.root["invocation_id"], f"source-turn:{index}")
        elif previous.get("active_attempt_id") == phase.root["invocation_id"] and previous.get(
            "active_step_id"
        ):
            step_id, index = previous["active_step_id"], previous["step_index"]
    status, fields = phase.state or (invocation["status"], {})
    state = dict(status=status, **fields)
    if "provider_id" in state:
        state["provider_response_id"] = state.pop("provider_id")
    if "output_ref" in state:
        state["output_record_id"] = str(state.pop("output_ref").record_id)
    intent = by_kind.get("source_context_count_intent", by_kind.get("count_intent"))
    receipt = by_kind.get("source_context_count", by_kind.get("count_response"))
    count_error = by_kind.get("source_context_count_error")
    data = dict(state=state) if phase.state is not None else {}
    if phase.physical is not None:
        data["physical"] = phase.physical
    if getattr(phase, "rejection", None):
        data["rejection"] = phase.rejection
        data["submit_entry_id"] = str(previous["submit_entry_id"])
    if getattr(phase, "provider_calls", None) is not None:
        data["provider_calls"] = [
            {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()}
            for row in phase.provider_calls
        ]
    if phase.legacy_counts:
        data["legacy_counts"] = phase.legacy_counts
    observation = 0
    late_receipt = False
    submit_operation = operation_id(step_id, "submit")
    operation = submit_operation
    if getattr(phase, "structured_decision", None) is not None:
        decision = phase.structured_decision
        kind, operation, status = "structured_decision", decision["operation_id"], previous["phase"]
        data.update(
            provider_entry_id=str(decision["provider_entry_id"]), provider_calls=decision["provider_calls"]
        )
    elif getattr(phase, "semantic_result", None) is not None:
        kind, status, step_id, index = "attempt_output", "completed", None, None
        operation = operation_id(phase.root["invocation_id"], "output")
    elif "source_tools_final_response" in kinds or (
        phase.owner is phase.journal
        and phase.state is not None
        and status == "response_completed"
        and not {"provider_response", "source_tool_result", "source_tool_call"} & kinds
    ):
        kind, step_id, index = "attempt_response", None, None
        operation = operation_id(phase.root["invocation_id"], "response_ready")
    elif intent:
        kind = "count_receipt_count_intent" if receipt else "count_intent"
        identity = intent["data"].get("count_identity", intent["data"].get("request_blob", {}).get("sha256"))
        operation, status = operation_id(step_id, "count", identity), "counting"
        data["count_identity"] = identity
        if intent["data"].get("count_operation_id"):
            operation = UUID(intent["data"]["count_operation_id"])
        if intent["data"].get("retry_error_ref"):
            data["retry_error_ref"] = intent["data"]["retry_error_ref"]
    elif count_error and count_error["data"].get("retryable") is True:
        kind, status = "count_error", "prepared"
        operation = UUID(count_error["data"]["count_operation_id"])
        data["count_intent_ref"] = count_error["data"]["count_intent_ref"]
        data["count_identity"] = count_error["data"]["count_identity"]
    elif "submit_intent" in kinds:
        kind, status = ("count_receipt_submit_intent" if receipt else "submit_intent"), "submitting"
    elif "provider_response" in kinds:
        kind = "provider_receipt"
        observation, late_receipt = bind(phase, step_id, operation, data)
    elif "source_tool_result" in kinds or "source_tool_call" in kinds:
        kind = "tool_result" if "source_tool_result" in kinds else "tool_intent"
        tool = by_kind.get("source_tool_result", by_kind.get("source_tool_call"))["data"]
        data.update(call_id=tool["call_id"], tool_name=tool["name"], arguments_sha256=tool["arguments_hash"])
        operation = operation_id(
            step_id,
            "tool",
            tool["provider_response_ref"]["record_id"],
            tool["call_id"],
            tool["arguments_hash"],
        )
        status = "tool_running"
    elif receipt:
        kind, status = "count_receipt", status if status in ("failed", "validation_failed") else "counted"
        operation = operation_id(
            step_id, "count", receipt["data"].get("count_identity", previous.get("count_identity"))
        )
        if receipt["data"].get("count_operation_id"):
            operation = UUID(receipt["data"]["count_operation_id"])
    elif phase.state is not None and status in ("failed", "validation_failed", "outcome_unknown"):
        kind = "attempt_failed"
        operation = operation_id(step_id, "failure", sorted(kinds), previous.get("revision", 0))
    else:
        kind = "observation"
        operation = operation_id(step_id, "observation", sorted(kinds), previous.get("revision", 0))
        status = previous.get("phase", status)
    if kind in ("tool_intent", "tool_result"):
        data["state"] = dict(status="response_completed")
    elif kind == "count_receipt" and "state" not in data:
        data["state"] = dict(status=status)
        if type(receipt["data"].get("input_tokens")) is int:
            data["state"]["input_tokens"] = receipt["data"]["input_tokens"]
    parents, objects, source_entries = resolve(phase, kind, data, by_kind)
    bound = journal.execution.prepare(phase.run_id)
    command = Checkpoint(
        run_id=phase.run_id,
        attempt_id=phase.root["invocation_id"],
        operation_id=operation,
        kind=kind,
        input_set_id=bound["set_id"],
        expected_revision=previous.get("revision", 0),
        data=data,
        step_id=step_id,
        step_index=index,
        observation_no=observation,
        views=tuple(phase.views),
        objects=objects,
        parents=tuple(sorted(parents, key=str)),
        head=HeadChange(phase=status, provider_id=fields.get("provider_id")),
        action=action,
    )
    passive_observation = (
        kind == "observation"
        and phase.state is None
        and phase.physical is None
        and inactive_owner(phase, step_id)
    )
    if kind == "attempt_response" or late_receipt or passive_observation:
        command = replace(command, head=HeadChange(preserve_active=True))
    if kind == "provider_receipt" and not late_receipt:
        command = replace(command, head=replace(command.head, provider_entry_id=command.entry_id))
    if "submit_intent" in kinds:
        command = replace(command, head=replace(command.head, submit_entry_id=command.entry_id))
    if intent:
        count_identity = intent["data"].get(
            "count_identity", intent["data"].get("request_blob", {}).get("sha256")
        )
        command = replace(command, head=replace(command.head, count_identity=count_identity))
    if receipt:
        command = replace(command, head=replace(command.head, count_entry_id=command.entry_id))
    if intent and not data.get("retry_error_ref") or phase.physical is not None:
        command = replace(command, head=replace(command.head, request_entry_id=command.entry_id))
    if getattr(phase, "semantic_result", None) is not None:
        command = replace(
            command,
            semantic_result=replace(
                phase.semantic_result,
                source_entries=tuple(sorted({command.entry_id, *source_entries}, key=str)),
            ),
        )
    from .checkpoint_accounting import compile_charges

    return compile_charges(phase, command, by_kind, intent, receipt)
