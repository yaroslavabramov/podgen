"""Prove a recorded local size rejection occurred before any generation submission."""

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.writing.projections import CONTRACT

from .contracts import RunFailure
from .validation import load_unique_json
from .wire_budget import visible_sizes


def request_record(journal, row):
    return journal.read(RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"]))


def unsent_evidence(journal, row):
    def reject():
        raise RunFailure(
            "input_budget_retry_not_eligible", run_id=row["run_id"], invocation_id=row["invocation_id"]
        )

    if (
        row["status"] != "validation_failed"
        or row["operation_kind"] != "role"
        or any(row.get(k) is not None for k in ("provider_response_id", "output_record_id", "output_tokens"))
    ):
        reject()
    if request_record(journal, row)["context"].get("role_input_contract") != CONTRACT:
        reject()
    events = {}
    for event in journal.events(row):
        ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = journal.read(ref)
        if record["invocation_id"] != str(row["invocation_id"]) or record["request_ref"]["record_id"] != str(
            row["request_record_id"]
        ):
            reject()
        kind = event["event_kind"]
        if (
            kind not in ("failure", "role_input_measurement", "count_intent", "count_response")
            or kind in events
        ):
            reject()
        events[kind] = (ref, record)
    if not {"failure", "role_input_measurement"} <= events.keys():
        reject()
    failure_ref, failure = events["failure"]
    measurement = events["role_input_measurement"][1]["data"]
    measured = visible_sizes(journal.request(row))
    if measurement["visible_bytes"] != measured or failure["data"].get("status") != "validation_failed":
        reject()
    code = failure["data"].get("code")
    if code == "role_visible_byte_budget_exceeded":
        if (
            len(events) != 2
            or row.get("input_tokens") is not None
            or measured["total"] <= measurement["limits"]["visible_bytes"]
        ):
            reject()
    elif code == "role_input_token_budget_exceeded":
        if (
            len(events) != 4
            or type(row.get("input_tokens")) is not int
            or row["input_tokens"] <= measurement["limits"]["input_tokens"]
        ):
            reject()
        ref, response = events["count_response"]
        count = load_unique_json(journal.read_blob(ref, BlobRef.model_validate(response["response_blob"])))
        if count.get("input_tokens") != row["input_tokens"]:
            reject()
    else:
        reject()
    return failure_ref, measured
