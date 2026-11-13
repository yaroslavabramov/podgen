"""Small control indexes, never substitutes for persisted working state or source reads."""

from dataclasses import dataclass

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.domain.references import walk_values
from podgen.persistence.read_authorization import authorized_read

FIELDS = {
    "source_context_count": ("request_hash", "input_tokens", "turn_index", "label"),
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
    return EventMetadata(
        reference,
        kind,
        str(invocation_id),
        {key: data[key] for key in FIELDS.get(kind, ()) if key in data},
        receipts,
    )


def from_record(reference, record):
    return project(reference, record["event_kind"], record["invocation_id"], record["data"])


def stream(journal, roots):
    """Authorize once per bounded restore, and discard full event bodies as we go.

    The existing graph reader remains authoritative. Its common provenance memo
    lives only inside this local read-only snapshot, never across provider calls.
    Full event bodies (including checkpoint arguments and source passages) are
    removed from that memo after projection, so restore retains only references.
    """
    events = [(root, event) for root in roots for event in journal.events(root)]
    event_ids = {event["record_id"] for _, event in events}
    repo = journal.repo
    with repo.database.read_transaction() as tx, authorized_read(repo, tx):
        journal.authorize(tx)
        memo = repo._read_authorization.get().completed
        for root, event in events:
            reference = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
            record = repo._runtime(tx, journal.project, reference)["payload"]
            if (
                record["event_kind"] != event["event_kind"]
                or record["invocation_id"] != str(root["invocation_id"])
                or record["request_ref"]["record_id"] != str(root["request_record_id"])
            ):
                raise Conflict("research_history_event_binding_changed")
            metadata = from_record(reference, record)
            # A segment can authorize a previous checkpoint event as a linked
            # dependency. Discard those full bodies too; their graph ancestors
            # remain memoized, while later raw reads still reauthorize normally.
            linked_nodes = [reference] + [
                value for _, _, value in walk_values(record) if isinstance(value, RuntimeRef)
            ]
            for linked in linked_nodes:
                key = ("runtime", journal.project, linked.record_kind, linked.record_id)
                cached = memo.get(key)
                payload = cached["payload"] if cached is not None else {}
                physical_request = payload.get(
                    "contract"
                ) == "model-request-v1" and "source_tool_turn" in payload.get("context", {})
                if linked.record_id in event_ids or physical_request:
                    memo.pop(key, None)
            yield metadata
