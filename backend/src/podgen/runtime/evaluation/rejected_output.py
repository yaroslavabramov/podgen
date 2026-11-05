"""Read a complete rejected role response without changing its content or trusting a new model call."""

from podgen.domain.contracts.common import BlobRef, RuntimeRef

from .contracts import RunFailure
from .validation import load_unique_json


def rejected_output(journal, original, model, *, code):
    if (
        original["status"] != "validation_failed"
        or original["operation_kind"] != "role"
        or not original["provider_response_id"]
    ):
        raise RunFailure(code)
    request = journal.request(original)
    events = [event for event in journal.events(original) if event["event_kind"] == "provider_response"]
    if not events:
        raise RunFailure(code)
    ref = RuntimeRef(record_kind="tool_execution", record_id=events[-1]["record_id"])
    record = journal.read(ref)
    try:
        response = load_unique_json(journal.read_blob(ref, BlobRef.model_validate(record["response_blob"])))
        if (
            response["status"] != "completed"
            or response["id"] != original["provider_response_id"]
            or response["model"] != request["model"]
            or response["metadata"] != request["metadata"]
        ):
            raise ValueError()
        parts = []
        for item in response["output"]:
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item["role"] != "assistant":
                raise ValueError()
            for content in item["content"]:
                if content["type"] != "output_text":
                    raise ValueError()
                parts.append(content["text"])
        text = "".join(parts)
        load_unique_json(text)
        return model.model_validate_json(text), request, ref
    except (ValueError, TypeError, KeyError):
        raise RunFailure(code) from None
