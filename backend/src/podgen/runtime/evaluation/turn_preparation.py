"""Stage physical request and its projection for the next explicit publication phase."""

from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import query


def stage_prepare(owner, body, *, pending_events=(), admission_refs=(), original_record=None):
    with owner.journal.repo.database.transaction() as tx:
        owner.journal.authorize(tx)
        previous = owner._row(tx)
    if previous is not None:
        if owner.journal.request(previous) != body:
            raise Conflict("source_tool_turn_request_changed")
        return previous, None, None
    identity = uuid5(owner.root["invocation_id"], f"source-turn:{owner.index}")
    import json

    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid5(identity, "request"))
    turn = dict(
        owner.root,
        invocation_id=identity,
        root_invocation_id=owner.root["invocation_id"],
        turn_index=owner.index,
        status="prepared",
        request_record_id=ref.record_id,
        input_tokens=None,
        output_tokens=None,
        provider_response_id=None,
        output_record_id=None,
        attempt_index=0,
    )
    phase = owner.publication(turn)
    # Context plan precedes the physical request in the historical event stream.
    for kind, data in pending_events:
        root_phase = owner.journal.publication(owner.root)
        root_phase.event(kind, data=data, history=owner.history)
        phase.records.extend(root_phase.records)
        phase.events.extend(root_phase.events)
        phase.committed.extend(root_phase.committed)
    blob = phase.blob(json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    original = (
        original_record
        if original_record is not None
        else owner.journal.read(
            RuntimeRef(record_kind="tool_execution", record_id=owner.root["request_record_id"])
        )
    )
    context = dict(
        original["context"],
        source_tool_turn=dict(root_invocation_id=str(owner.root["invocation_id"]), turn_index=owner.index),
    )
    if admission_refs:
        context["runtime_admission_refs"] = [ref.model_dump(mode="json") for ref in admission_refs]
    record = dict(
        contract="model-request-v1",
        invocation_id=str(identity),
        run_id=str(owner.root["run_id"]),
        stage=owner.root["stage"],
        request_blob=blob.model_dump(mode="json"),
        request_hash=blob.sha256,
        context=context,
    )
    phase.record(record, record_id=ref.record_id, replay=True)

    def insert(tx):
        query(
            tx,
            """INSERT INTO evaluation_model_turns
               (project_id,root_invocation_id,turn_index,invocation_id,status,request_record_id)
               VALUES(:p,:i,:n,:t,'prepared',:r)""",
            p=owner.journal.project,
            i=owner.root["invocation_id"],
            n=owner.index,
            t=identity,
            r=ref.record_id,
        )

    phase.writes.append(insert)
    return turn, phase, (body, record)
