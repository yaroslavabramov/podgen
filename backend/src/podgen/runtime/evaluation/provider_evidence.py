"""Authenticate saved provider decisions for legacy continuation and structured research."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.execution.provider_calls import project_calls


def adopt_response(journal, root, event):
    if event.get("journal_version") == "execution-journal/3":
        return
    ref = RuntimeRef(record_kind="tool_execution", record_id=event["_record_id"])
    with journal.read_operation():
        record = journal.read(ref)
        if record.get("event_kind") != "provider_response" or record.get("invocation_id") != str(
            root["invocation_id"]
        ):
            raise Conflict("legacy_provider_response_binding_mismatch")
        raw = journal.read_blob(ref, BlobRef.model_validate(record["response_blob"]))
    from .legacy_receipts import adopt

    rows = project_calls(journal.project, root["run_id"], ref, record, raw)
    return adopt(journal, root, ref, record, raw, rows)


def register_structured(journal, root, event, requests, calls, turn, history):
    """Bind explicit structured research decisions to their saved response bytes."""
    from podgen.persistence.execution.provider_calls import project_call_rows
    from podgen.runtime.evaluation.validation import load_unique_json
    from podgen.runtime.models.research_continuation import structured_call, structured_requests

    from .source_tool_contract import anchored_research, from_manifest

    contract = from_manifest(journal.manifest(root["run_id"]))
    anchored = contract is not None and anchored_research(contract, root["stage"])

    ref = RuntimeRef(record_kind="tool_execution", record_id=event["_record_id"])
    with journal.read_operation():
        record = journal.read(ref)
        received = load_unique_json(journal.read_blob(ref, BlobRef.model_validate(record["response_blob"])))
    if (
        record.get("event_kind") != "provider_response"
        or record.get("invocation_id") != str(root["invocation_id"])
        or record.get("data", {}).get("source_turn_index") != turn
        or digest(structured_requests(received, turn, history)) != digest(requests)
        or digest(
            [
                structured_call(request, turn, position, anchored=anchored)
                for position, request in enumerate(requests)
            ]
        )
        != digest(calls)
    ):
        raise Conflict("structured_research_decision_binding_mismatch")
    adopted = adopt_response(journal, root, event)
    rows = project_call_rows(journal.project, root["run_id"], ref, record, calls)
    from uuid import UUID

    from podgen.persistence.database import one
    from podgen.persistence.private_journal.contracts import operation_id
    from podgen.persistence.private_journal.structured import existing
    from podgen.persistence.read_authorization import _read_connection

    with _read_connection(journal.repo) as tx:
        parent = one(
            tx,
            "SELECT entry_id,step_id FROM execution_entries WHERE project_id=:p AND archive_ids @> ARRAY[CAST(:id AS uuid)]",
            p=journal.project,
            id=ref.record_id,
        )
    if parent is None:
        if adopted is None:
            raise Conflict("legacy_structured_receipt_requires_adoption")
        parent = dict(
            entry_id=adopted, step_id=UUID(record["data"].get("source_turn_id", record["invocation_id"]))
        )
    normalized = [
        {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()} for row in rows
    ]
    operation = operation_id(parent["step_id"], "structured_decision", str(ref.record_id), digest(calls))
    found = existing(journal.repo, journal.project, root["run_id"], operation, parent["entry_id"], normalized)
    if found is not None:
        return found
    phase = journal.publication(root)
    phase.structured_decision = dict(
        provider_response_ref=ref,
        provider_entry_id=parent["entry_id"],
        provider_calls=normalized,
        operation_id=operation,
    )
    phase.commit()
    return phase.entry_id
