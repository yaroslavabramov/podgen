"""Structured research requests retain actual provider decision proofs on SQL storage."""

import hashlib
import json
from uuid import uuid4

import pytest

from podgen.domain.errors import Conflict
from podgen.persistence.database import query
from podgen.runtime.evaluation.provider_evidence import register_structured
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.research_continuation import structured_call, structured_requests
from podgen.runtime.models.research_history import ResearchHistory
from tests.test_model_step_publication import setup


@pytest.mark.parametrize("legacy", [False, True])
def test_structured_decision_proof_is_derived_from_full_saved_response(repo, scope, legacy):
    journal, root, tj, turn, phase, *_ = setup(repo, scope)
    phase.commit()
    request = dict(question="Exact missing question?", why_needed="Verify the original account.")
    response = dict(
        output=[
            dict(
                type="message",
                content=[dict(type="output_text", text=json.dumps(dict(research_requests=[request])))],
            )
        ]
    )
    raw = json.dumps(response).encode()
    if legacy:
        binding, data = tj.event_binding(turn, {})
        blob = journal.blob(raw)
        event = journal.record(
            dict(
                contract="model-invocation-event-v1",
                invocation_id=str(root["invocation_id"]),
                event_kind="provider_response",
                request_ref=dict(record_kind="tool_execution", record_id=str(root["request_record_id"])),
                data=data,
                response_blob=blob.model_dump(mode="json"),
            )
        )
        with repo.database.transaction() as tx:
            query(
                tx,
                """INSERT INTO evaluation_invocation_events
                (project_id,invocation_id,event_id,record_id,event_kind,source_turn_index)
                VALUES(:p,:i,:e,:id,'provider_response',0)""",
                p=scope[0],
                i=root["invocation_id"],
                e=uuid4(),
                id=event.record_id,
            )
    else:
        phase = tj.publication(turn)
        event = phase.event("provider_response", reply=WireReply(raw))
        phase.commit()
    original = journal.read(event)
    history = ResearchHistory(journal, root)
    saved = history.provider_response(0)
    requests = structured_requests(response, 0, history)
    calls = [structured_call(request, 0, 0)]
    register_structured(journal, root, saved, requests, calls, 0, history)
    data = dict(
        turn_index=0,
        call_index=0,
        call_id=calls[0]["call_id"],
        name="request_research",
        raw_arguments=calls[0]["arguments"],
        arguments=json.loads(calls[0]["arguments"]),
        arguments_hash=hashlib.sha256(calls[0]["arguments"].encode()).hexdigest(),
        provider_response_ref=event.model_dump(mode="json"),
    )
    phase = journal.publication(root)
    phase.event("source_tool_call", data=data)
    phase.commit()
    phase = journal.publication(root)
    result = phase.event("source_tool_result", data=dict(data, result=dict(synthetic=True)))
    phase.commit()
    assert journal.read(result)["data"]["result"] == dict(synthetic=True)
    assert journal.read(event) == original
    forged = [dict(calls[0], arguments='{"question":"invented"}')]
    with pytest.raises(Conflict, match="structured_research_decision_binding_mismatch"):
        register_structured(journal, root, saved, requests, forged, 0, history)
