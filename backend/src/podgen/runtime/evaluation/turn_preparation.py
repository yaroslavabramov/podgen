"""Physical candidate bytes join their first count/submit checkpoint."""

import json
from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict

from .physical_turn import descriptor, invocation


def stage_candidate(phase, body, index, original, *, admission_refs=()):
    root = phase.root
    identity = uuid5(root["invocation_id"], f"source-turn:{index}")
    blob = phase.blob(json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid5(identity, f"request/3/{blob.sha256}"))
    turn = dict(
        root,
        project_id=phase.journal.project,
        invocation_id=identity,
        root_invocation_id=root["invocation_id"],
        turn_index=index,
        status="prepared",
        request_record_id=ref.record_id,
        input_tokens=None,
        output_tokens=None,
        provider_response_id=None,
        output_record_id=None,
        attempt_index=0,
    )
    context = dict(
        original["context"],
        source_tool_turn=dict(root_invocation_id=str(root["invocation_id"]), turn_index=index),
    )
    record = dict(
        contract="model-request-v1",
        invocation_id=str(identity),
        run_id=str(root["run_id"]),
        stage=root["stage"],
        request_blob=blob.model_dump(mode="json"),
        request_hash=blob.sha256,
        context=context,
    )
    overrides = dict(source_tool_turn=context["source_tool_turn"])
    if admission_refs:
        overrides["runtime_admission_refs"] = [ref.model_dump(mode="json") for ref in admission_refs]
        record["context"].update(overrides)
    canonical = {key: value for key, value in record.items() if key != "context"}
    canonical.update(
        context_parent_ref=RuntimeRef(
            record_kind="tool_execution", record_id=root["request_record_id"]
        ).model_dump(mode="json"),
        context_overrides=overrides,
    )
    phase.record(canonical, record_id=ref.record_id, replay=True, projection="physical-request/3")
    phase.physical, phase.request_snapshot = descriptor(turn), (body, record)
    return turn, (body, record)


def stage_prepare(owner, body, *, pending_events=(), admission_refs=(), original_record=None):
    execution = owner.journal.execution
    cached = execution.current_physical(owner.root["run_id"], owner.root["invocation_id"], owner.index)
    previous = invocation(owner.root, cached[0]) if cached else owner._row()
    if previous is not None:
        snapshot = cached[1] if cached else None
        saved_body = snapshot[0] if snapshot else owner.journal.request(previous)
        if saved_body != body:
            raise Conflict("source_tool_turn_request_changed")
        if pending_events:
            phase = owner.publication(previous)
            stage_events(owner, phase, pending_events)
            return previous, phase, snapshot
        return previous, None, snapshot
    execution.prepare(owner.root["run_id"], evaluation=admission_refs)
    phase = owner.publication(owner.root)
    stage_events(owner, phase, pending_events)
    original = (
        original_record
        if original_record is not None
        else owner.journal.read(
            RuntimeRef(record_kind="tool_execution", record_id=owner.root["request_record_id"])
        )
    )
    turn, snapshot = stage_candidate(phase, body, owner.index, original, admission_refs=admission_refs)
    phase.invocation = turn
    return turn, phase, snapshot


def stage_events(owner, phase, events):
    if not events:
        return
    root_phase = owner.journal.publication(owner.root)
    for kind, data in events:
        root_phase.event(kind, data=data, history=owner.history)
    phase.absorb(root_phase)
