"""Authenticate saved provider decisions for legacy continuation and structured research."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.execution.provider_calls import project_calls


def adopt_response(journal, root, event):
    if event.get("journal_version") == "execution-journal/2":
        return
    ref = RuntimeRef(record_kind="tool_execution", record_id=event["_record_id"])
    with journal.read_operation():
        record = journal.read(ref)
        if record.get("event_kind") != "provider_response" or record.get("invocation_id") != str(
            root["invocation_id"]
        ):
            raise Conflict("legacy_provider_response_binding_mismatch")
        raw = journal.read_blob(ref, BlobRef.model_validate(record["response_blob"]))
    # These are actual historical control parents, not an arbitrary same-run grant.
    references = [ref, RuntimeRef.model_validate(record["request_ref"])]
    physical = record.get("data", {}).get("turn_request_ref")
    if physical is not None:
        references.append(RuntimeRef.model_validate(physical))
    journal.execution.prepare(root["run_id"], evaluation=references)
    rows = project_calls(journal.project, root["run_id"], ref, record, raw)
    journal.execution.publish(lambda tx: insert_rows(tx, "execution_provider_calls", rows, on_conflict=True))


def register_structured(journal, root, event, requests, calls, turn, history):
    """Bind explicit structured research decisions to their saved response bytes."""
    from podgen.persistence.execution.live import authorize
    from podgen.persistence.execution.parents import authorize_parents
    from podgen.persistence.execution.provider_calls import project_call_rows
    from podgen.runtime.evaluation.validation import load_unique_json
    from podgen.runtime.models.research_continuation import structured_call, structured_requests

    ref = RuntimeRef(record_kind="tool_execution", record_id=event["_record_id"])
    with journal.read_operation():
        record = journal.read(ref)
        received = load_unique_json(journal.read_blob(ref, BlobRef.model_validate(record["response_blob"])))
    if (
        record.get("event_kind") != "provider_response"
        or record.get("invocation_id") != str(root["invocation_id"])
        or record.get("data", {}).get("source_turn_index") != turn
        or digest(structured_requests(received, turn, history)) != digest(requests)
        or digest([structured_call(request, turn, position) for position, request in enumerate(requests)])
        != digest(calls)
    ):
        raise Conflict("structured_research_decision_binding_mismatch")
    adopt_response(journal, root, event)
    rows = project_call_rows(journal.project, root["run_id"], ref, record, calls)

    def write(tx):
        bound = journal.execution.sets[root["run_id"]]
        authorize(journal.repo, tx, journal.project, bound["set_id"])
        authorize_parents(journal.repo, tx, journal.project, [ref.record_id], include_parents=False)
        insert_rows(tx, "execution_provider_calls", rows, on_conflict=True)

    journal.execution.publish(write)
