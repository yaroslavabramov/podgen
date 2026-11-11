"""Prove a transient count failure happened before generation, without relaxing submit evidence."""

from podgen.domain.contracts.common import BlobRef, RuntimeRef

from .compiler import COUNT_FIELDS
from .validation import load_unique_json


def count_failure_evidence(journal, row):
    if row["status"] != "failed" or any(
        row.get(key) is not None
        for key in ("provider_response_id", "output_record_id", "input_tokens", "output_tokens")
    ):
        return None
    records = {}
    for event in journal.events(row):
        kind = event["event_kind"]
        if kind not in ("role_input_measurement", "count_intent", "provider_error") or kind in records:
            return None
        ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = journal.read(ref)
        if record["invocation_id"] != str(row["invocation_id"]) or record["request_ref"]["record_id"] != str(
            row["request_record_id"]
        ):
            return None
        records[kind] = ref, record
    if not {"count_intent", "provider_error"} <= records.keys():
        return None
    failure_ref, failure = records["provider_error"]
    data = failure["data"]
    code = data.get("code", "")
    if (
        data.get("operation") != "count"
        or data.get("ambiguous") is not False
        or not (
            code in ("provider_transport_error", "provider_http_408") or code.startswith("provider_http_5")
        )
    ):
        return None
    count_ref, count = records["count_intent"]
    try:
        body = load_unique_json(
            journal.read_blob(count_ref, BlobRef.model_validate(count["data"]["request_blob"]))
        )
    except (ValueError, KeyError, TypeError):
        return None
    if body != {key: value for key, value in journal.request(row).items() if key in COUNT_FIELDS}:
        return None
    return dict(
        count_event_ref=count_ref.model_dump(mode="json"),
        failure_event_ref=failure_ref.model_dump(mode="json"),
    )
