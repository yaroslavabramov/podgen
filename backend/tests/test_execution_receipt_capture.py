"""Malformed optional call projections must never erase received provider bytes."""

import json

import pytest

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.persistence.database import query
from podgen.runtime.models.contracts import WireReply
from tests.test_model_step_publication import setup


@pytest.mark.parametrize(
    "malformed", ["nul_id", "nul_name", "surrogate_id", "surrogate_name", "oversized_id", "duplicate"]
)
def test_full_raw_response_survives_unsupported_call_projection(repo, scope, malformed):
    journal, root, tj, turn, phase, _, _, _, _, _ = setup(repo, scope)
    phase.commit()
    call = dict(type="function_call", call_id="synthetic_call", name="read_source", arguments="{}")
    if malformed == "nul_id":
        call["call_id"] = "bad\0id"
    elif malformed == "nul_name":
        call["name"] = "bad\0name"
    elif malformed == "surrogate_id":
        call["call_id"] = "bad\ud800id"
    elif malformed == "surrogate_name":
        call["name"] = "bad\ud800name"
    elif malformed == "oversized_id":
        call["call_id"] = "large-id-" * 2000
    output = [call, call] if malformed == "duplicate" else [call]
    raw = json.dumps(dict(id="resp_synthetic", output=output), ensure_ascii=True).encode()
    phase = tj.publication(turn)
    ref = phase.event("provider_response", reply=WireReply(raw))
    phase.commit()
    record = journal.read(ref)
    assert journal.read_blob(ref, BlobRef.model_validate(record["response_blob"])) == raw
    with repo.database.read_transaction() as tx:
        proof = query(
            tx,
            "SELECT 1 FROM execution_provider_calls WHERE project_id=:p AND response_record_id=:id",
            p=scope[0],
            id=ref.record_id,
        ).first()
    assert proof is None
    assert journal.read(RuntimeRef(record_kind="tool_execution", record_id=turn["request_record_id"]))


@pytest.mark.parametrize("malformed", ["duplicate_response", "invalid_wire", "duplicate_wire", "wrong_wire"])
def test_pending_count_strict_validation_keeps_raw_receipt_and_failure(repo, scope, malformed):
    from types import SimpleNamespace

    from podgen.domain.errors import Conflict
    from podgen.runtime.models.gateway import ModelGateway
    from podgen.runtime.models.research_context import ResearchContext
    from podgen.runtime.models.research_history import ResearchHistory
    from tests.test_model_step_publication import run

    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    profile.provider = "openai"
    response = (
        b'{"input_tokens":100,"input_tokens":100}'
        if malformed == "duplicate_response"
        else b'{"input_tokens":100}'
    )
    wire = {
        "invalid_wire": b'{"model":',
        "duplicate_wire": b'{"model":"a","model":"b"}',
        "wrong_wire": b"null",
    }.get(malformed)
    reply = WireReply(response, request_body=wire)
    provider = SimpleNamespace(
        count=lambda body: reply, submit=lambda *args, **kwargs: pytest.fail("invalid count proof submitted")
    )
    history = ResearchHistory(journal, root)
    context = ResearchContext(
        ModelGateway(journal, lambda _: provider), root, profile, snapshot[0], history, lambda: None
    )
    context.measured(snapshot[0], 0, "normal")
    receipt = context.receipt(snapshot[0])
    with pytest.raises(Conflict):
        run(
            ModelGateway(tj, lambda _: provider),
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
            count_receipt=receipt,
        )
    saved = journal.read(receipt.reference)
    assert journal.read_blob(receipt.reference, BlobRef.model_validate(saved["response_blob"])) == response
    if wire is not None:
        assert (
            journal.read_blob(receipt.reference, BlobRef.model_validate(saved["wire_request_blob"])) == wire
        )
    kinds = [event["event_kind"] for event in journal.events(root)]
    assert "failure" in kinds and "submit_intent" not in kinds
