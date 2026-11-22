"""Small control indexes, never substitutes for persisted working state or source reads."""

from dataclasses import dataclass

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict

FIELDS = {
    "source_web_search": ("turn_index",),
    "source_context_count_error": ("count_identity", "turn_index", "retryable"),
    "source_context_count": ("request_hash", "input_tokens", "turn_index", "label", "count_identity"),
    "source_context_plan": ("turn_index", "conversation_hash", "checkpoint"),
    "source_context_segment": ("next_turn", "checkpoint_call_index"),
    "submit_intent": ("source_turn_index",),
    "provider_response": ("source_turn_index",),
    "source_tool_call": ("turn_index", "call_index", "call_id", "name"),
    "source_tool_result": ("turn_index", "call_index", "call_id", "name"),
    "source_structured_material_request": ("turn_index", "request_hash"),
}


@dataclass(frozen=True)
class EventMetadata:
    reference: RuntimeRef
    kind: str
    invocation_id: str
    data: dict
    receipt_ids: tuple[str, ...] = ()


def project(reference, kind, invocation_id, data):
    result = data.get("result", {}) if kind == "source_tool_result" else {}
    receipts = tuple(result[field]["read_id"] for field in ("passage", "material_passage") if field in result)
    compact = {key: data[key] for key in FIELDS.get(kind, ()) if key in data}
    if kind == "source_tool_result" and data.get("name") == "search_sources":
        from podgen.writing.research_materials.fact_navigation import VERSION

        delivery = data.get("search_delivery_identity") or {}
        if delivery.get("contract") == VERSION:
            compact["shown_search_passages"] = delivery["passages"]
    return EventMetadata(
        reference,
        kind,
        str(invocation_id),
        compact,
        receipts,
    )


def from_record(reference, record):
    return project(reference, record["event_kind"], record["invocation_id"], record["data"])


def stream(journal, roots):
    """Restore authorized control indexes in bounded pages, not native/tool bodies.

    Private rows project only the fields needed by the indexes. Legacy records
    use the existing batch reader. Full contents remain behind their original
    reading boundary, and no authorization snapshot survives external work.
    """
    from .history_restore import records

    fields = {field for values in FIELDS.values() for field in values}
    for root in roots:
        for reference, kind, record in records(journal, root, fields):
            if (
                record["event_kind"] != kind
                or record["invocation_id"] != str(root["invocation_id"])
                or record["request_ref"]["record_id"] != str(root["request_record_id"])
            ):
                raise Conflict("research_history_event_binding_changed")
            metadata = from_record(reference, record)
            yield metadata
