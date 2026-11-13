"""Prove an exact Anthropic grammar rejection; never infer safety from HTTP 400 alone."""

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.runtime.models.messages_tool_policy import KEY, NAVIGATION, VERSION
from podgen.runtime.models.messages_wire import messages_request

from .validation import load_unique_json

MESSAGES = frozenset(
    {
        "The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or reduce the number of strict tools.",
        "Schema is too complex for compilation.",
    }
)


def evidence(journal, row):
    if row["status"] != "failed" or any(
        row.get(key) is not None
        for key in (
            "provider_response_id",
            "output_record_id",
            "output_tokens",
        )
    ):
        return None
    submits, failures = [], []
    for event in journal.events(row):
        if event["event_kind"] not in {
            "role_input_measurement",
            "count_intent",
            "count_response",
            "count_receipt_reused",
            "submit_intent",
            "provider_error",
        }:
            return None
        ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = journal.read(ref)
        if (
            record["event_kind"] != event["event_kind"]
            or record["invocation_id"] != str(row["invocation_id"])
            or record["request_ref"]["record_id"] != str(row["request_record_id"])
        ):
            return None
        if event["event_kind"] == "submit_intent":
            submits.append(record)
        elif event["event_kind"] == "provider_error":
            failures.append((ref, record))
    if (
        len(submits) != 1
        or len(failures) != 1
        or submits[0]["data"].get("client_request_id") != str(row["invocation_id"])
    ):
        return None
    ref, failure = failures[0]
    data = failure["data"]
    if (
        data.get("code") != "provider_http_400"
        or data.get("operation") != "submit"
        or data.get("ambiguous") is not False
        or failure.get("provider_http_status") != 400
        or failure.get("provider_response_id") is not None
        or data.get("provider_response_id") is not None
    ):
        return None
    try:
        response = load_unique_json(journal.read_blob(ref, BlobRef.model_validate(failure["response_blob"])))
        request = journal.request(row)
        native = load_unique_json(
            journal.read_blob(ref, BlobRef.model_validate(failure["wire_request_blob"]))
        )
        if (
            response.get("type") != "error"
            or response.get("id") is not None
            or response.get("response_id") is not None
            or response.get("usage") is not None
            or response["error"].get("type") != "invalid_request_error"
            or response["error"].get("message") not in MESSAGES
            or any(response["error"].get(key) is not None for key in ("id", "response_id", "usage"))
            or not request["model"].startswith("claude-")
            or KEY in request.get("metadata", {})
            or not any(
                tool.get("strict") is True and tool.get("name") in NAVIGATION
                for tool in native.get("tools", [])
            )
            or native != messages_request(request)
        ):
            return None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return dict(schema_refusal_event_ref=ref.model_dump(mode="json"), tool_policy=VERSION)
