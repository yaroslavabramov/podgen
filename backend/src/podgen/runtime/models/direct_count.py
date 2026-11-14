"""Bind direct model counts to native bytes and recover only authenticated receipts."""

import json

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.compiler import count_request
from podgen.runtime.evaluation.validation import load_unique_json

from .count_receipts import (
    CONTRACT,
    ExactCount,
    PendingExactCount,
    identity,
    native_count,
    provider_configuration,
)


def prepare(phase, body, profile, admission_refs):
    request = count_request(body)
    native = native_count(body, profile.provider)
    native_blob = phase.blob(_bytes(native))
    data = dict(
        request_blob=phase.blob(_bytes(request)).model_dump(mode="json"),
        count_contract=CONTRACT,
        count_identity=identity(body, profile),
        native_request_blob=native_blob.model_dump(mode="json"),
        provider_configuration=provider_configuration(profile),
    )
    if admission_refs:
        data["runtime_admission_refs"] = [ref.model_dump(mode="json") for ref in admission_refs]
    intent = phase.event("count_intent", data=data)
    return request, intent, native_blob, data


def capture(phase, intent, native_blob, data, reply, body, profile):
    # Always stage the received body before optional parsing or wire validation.
    phase.reuse_blob(native_blob, intent)
    evidence = {
        key: data[key]
        for key in ("count_contract", "count_identity", "native_request_blob", "provider_configuration")
    }
    evidence["count_intent_ref"] = intent.model_dump(mode="json")
    try:
        count = load_unique_json(reply.body)["input_tokens"]
        valid = type(count) is int and 0 <= count <= 2**63 - 1
    except (ValueError, KeyError, TypeError):
        valid, count = False, None
    if valid:
        evidence["input_tokens"] = count
    reference = phase.event("count_response", data=evidence, reply=reply)
    if not valid:
        raise ContractError("invalid_input_token_count")
    receipt = PendingExactCount(
        reference,
        evidence["count_identity"],
        count,
        phase,
        native_count(body, profile.provider),
        provider_configuration(profile),
        reply,
    )
    return receipt.validate(phase.journal, body, profile)


def restored(journal, invocation, body, profile):
    owner = getattr(journal, "journal", journal)
    root = getattr(journal, "root", invocation)
    index = invocation.get("turn_index")
    events = owner.events_for_turn(root, index) if index is not None else owner.events(invocation)
    for position in range(len(events) - 1, -1, -1):
        event = events[position]
        if event["event_kind"] not in ("count_response", "count_receipt_reused"):
            continue
        reference = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = journal.read(reference)
        if event["event_kind"] == "count_receipt_reused":
            reference = RuntimeRef.model_validate(record["data"]["count_ref"])
            record = journal.read(reference)
        data = record["data"]
        if data.get("count_contract") == CONTRACT:
            return ExactCount(reference, data["count_identity"], invocation["input_tokens"])
        if record["event_kind"] == "source_context_count":
            from .legacy_count_receipts import LegacyExactCount

            return LegacyExactCount(reference, identity(body, profile), invocation["input_tokens"])
        from .legacy_direct_count import LegacyDirectCount

        intent = next(
            (row for row in reversed(events[:position]) if row["event_kind"] == "count_intent"), None
        )
        if intent is None:
            break
        return LegacyDirectCount(
            reference,
            identity(body, profile),
            invocation["input_tokens"],
            RuntimeRef(record_kind="tool_execution", record_id=intent["record_id"]),
        )
    raise Conflict("count_receipt_missing")


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
