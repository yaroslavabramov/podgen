"""Authenticate old provider control evidence once when a saved attempt continues."""

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
