"""Read a complete rejected role response without changing its content or trusting a new model call."""

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.runtime.models.response_content import binding_error, output_text

from .contracts import RunFailure
from .validation import load_unique_json


def saved_text(journal, original, *, code, events=None):
    """The exact output text of a completed, rejected role or anchor-selection response."""
    if (
        original["status"] != "validation_failed"
        or original["operation_kind"] not in ("role", "anchor_repair")
        or not original["provider_response_id"]
    ):
        raise RunFailure(code)
    request = journal.request(original)
    events = journal.events(original) if events is None else events
    event = next((e for e in reversed(events) if e["event_kind"] == "source_tools_final_response"), None)
    if event is None:
        event = next((e for e in reversed(events) if e["event_kind"] == "provider_response"), None)
    if event is None:
        raise RunFailure(code)
    ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
    record = journal.read(ref)
    try:
        response = load_unique_json(journal.read_blob(ref, BlobRef.model_validate(record["response_blob"])))
        if response["status"] != "completed" or binding_error(
            response, request, original["provider_response_id"]
        ):
            raise ValueError()
        text = output_text(
            response, web_search=any(t.get("type") == "web_search" for t in request.get("tools", []))
        )
        load_unique_json(text)
        return text, request, ref
    except (ValueError, TypeError, KeyError):
        raise RunFailure(code) from None


def rejected_output(journal, original, model, *, code):
    if original["operation_kind"] != "role":
        raise RunFailure(code)
    text, request, ref = saved_text(journal, original, code=code)
    try:
        return model.model_validate_json(text), request, ref
    except ValueError:
        raise RunFailure(code) from None
