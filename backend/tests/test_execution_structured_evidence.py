"""Prove structured decisions from captured bytes; pinned legacy cases live separately."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import query
from podgen.runtime.evaluation.provider_evidence import register_structured
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.research_continuation import structured_call, structured_requests
from podgen.runtime.models.research_history import ResearchHistory
from tests.test_model_step_publication import run, setup


def test_structured_decision_proof_is_derived_from_full_saved_response(repo, scope):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    request = dict(question="Exact missing question?", why_needed="Verify the original account.")

    def submit(body, **options):
        response = dict(
            id="resp_structured",
            model=body["model"],
            metadata=body["metadata"],
            status="completed",
            usage=dict(input_tokens=100, output_tokens=10),
            output=[
                dict(
                    type="message",
                    content=[dict(type="output_text", text=json.dumps(dict(research_requests=[request])))],
                )
            ],
        )
        return WireReply(json.dumps(response).encode())

    provider = SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'), submit=submit)
    response = run(
        ModelGateway(tj, lambda _: provider),
        turn,
        profile,
        limits,
        prepared_phase=phase,
        request_snapshot=snapshot,
        manifest_snapshot=manifest,
    )
    history = ResearchHistory(journal, root)
    saved = history.provider_response(0)
    reference = RuntimeRef(record_kind="tool_execution", record_id=saved["_record_id"])
    original = journal.read(reference)
    requests = structured_requests(response, 0, history)
    calls = [structured_call(request, 0, 0)]
    decision = register_structured(journal, root, saved, requests, calls, 0, history)
    data = dict(
        turn_index=0,
        call_index=0,
        call_id=calls[0]["call_id"],
        name="request_research",
        raw_arguments=calls[0]["arguments"],
        arguments=json.loads(calls[0]["arguments"]),
        arguments_hash=hashlib.sha256(calls[0]["arguments"].encode()).hexdigest(),
        provider_response_ref=reference.model_dump(mode="json"),
        decision_entry_id=str(decision),
    )
    phase = journal.publication(root)
    phase.event("source_tool_call", data=data)
    phase.commit()
    phase = journal.publication(root)
    result = phase.event("source_tool_result", data=dict(data, result=dict(synthetic=True)))
    phase.commit()
    assert journal.read(result)["data"]["result"] == dict(synthetic=True)
    assert journal.read(reference) == original
    forged = [dict(calls[0], arguments='{"question":"invented"}')]
    with pytest.raises(Conflict, match="structured_research_decision_binding_mismatch"):
        register_structured(journal, root, saved, requests, forged, 0, history)
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx, "SELECT count(*) FROM execution_provider_calls WHERE project_id=:p", p=scope[0]
            ).scalar_one()
            == 0
        )
