"""Malformed optional call projections must never erase received provider bytes."""

import json
from types import SimpleNamespace

import pytest

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.persistence.database import query
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from tests.test_model_step_publication import run, setup


@pytest.mark.parametrize(
    "malformed", ["nul_id", "nul_name", "surrogate_id", "surrogate_name", "oversized_id", "duplicate"]
)
def test_full_raw_response_survives_unsupported_call_projection(repo, scope, malformed):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
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
    raw = json.dumps(
        dict(
            id="resp_synthetic",
            model=snapshot[0]["model"],
            metadata=snapshot[0]["metadata"],
            status="completed",
            output=output,
            usage=dict(input_tokens=100, output_tokens=10),
        ),
        ensure_ascii=True,
    ).encode()
    provider = SimpleNamespace(
        count=lambda body: WireReply(b'{"input_tokens":100}'), submit=lambda *args, **kwargs: WireReply(raw)
    )
    run(
        ModelGateway(tj, lambda _: provider),
        turn,
        profile,
        limits,
        prepared_phase=phase,
        request_snapshot=snapshot,
        manifest_snapshot=manifest,
    )
    event = next(row for row in journal.events(root) if row["event_kind"] == "provider_response")
    ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
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
    from podgen.domain.errors import Conflict
    from podgen.runtime.models.research_context import ResearchContext
    from podgen.runtime.models.research_history import ResearchHistory

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
    turn, phase, snapshot = tj.stage_prepare(snapshot[0], original_record=context.original_record)
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


def test_pending_count_bytes_release_only_after_successful_absorbed_commit(repo, scope):
    from podgen.runtime.models.research_context import ResearchContext
    from podgen.runtime.models.research_history import ResearchHistory

    journal, root, tj, _, _, snapshot, _, profile, _, _ = setup(repo, scope)
    profile.provider = "openai"
    context = ResearchContext(
        ModelGateway(
            journal, lambda _: SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'))
        ),
        root,
        profile,
        snapshot[0],
        ResearchHistory(journal, root),
        lambda: None,
    )
    context.measured(snapshot[0], 0, "normal")
    receipt = context.receipt(snapshot[0])
    turn, phase, _ = tj.stage_prepare(snapshot[0], original_record=context.original_record)
    phase = phase or tj.publication(turn)
    phase.absorb(context.pending_phase, prepend=True)
    phase.transition("counted", input_tokens=receipt.input_tokens)

    from podgen.persistence.private_journal import writer

    original = writer.query

    def rollback(tx, sql, **params):
        if sql.startswith("UPDATE execution_heads SET"):
            raise RuntimeError("synthetic publication rollback")
        return original(tx, sql, **params)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(writer, "query", rollback)
        with pytest.raises(RuntimeError, match="synthetic publication rollback"):
            phase.commit()
    assert receipt.identity in context.pending_counts
    assert not phase.finished
    phase.commit()
    assert not context.pending_counts
    assert context.history.exact_count(receipt.identity) is not None
    from podgen.runtime.evaluation.journal import EvaluationJournal
    from podgen.runtime.evaluation.model_turns import TurnJournal

    fresh = EvaluationJournal(repo, scope[0])
    cold_root = fresh.invocation(root["run_id"], root["stage"])
    cold = TurnJournal(fresh, cold_root, 0)._row()
    assert cold["status"] == "counted" and cold["input_tokens"] == 100
