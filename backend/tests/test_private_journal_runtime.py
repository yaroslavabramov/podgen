"""Complete local provider/tool/resume/semantic cycle through the public facade."""

import json
from types import SimpleNamespace
from uuid import uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import one, query
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.model_turns import EventHistory
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.tool_gateway import _execute
from tests.test_model_step_publication import run, setup


@pytest.mark.parametrize("structured", [False, True])
def test_full_private_count_submit_tool_cold_resume_and_semantic_completion(repo, scope, structured):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, history = setup(repo, scope)
    calls = dict(count=0, submit=0, tool=0)
    call = dict(
        type="function_call", call_id="call_exact", name="read_source", arguments='{"query":"SYNTHETIC"}'
    )

    output = [call]
    if structured:
        from podgen.runtime.models.research_continuation import structured_call

        request = dict(question="SYNTHETIC exact source question", why_needed="Verify the original account")
        call = structured_call(request, 0, 0)
        output = [
            dict(
                type="message",
                content=[dict(type="output_text", text=json.dumps(dict(research_requests=[request])))],
            )
        ]

    def count(body):
        calls["count"] += 1
        return WireReply(b'{"input_tokens":100}')

    def submit(body, **options):
        calls["submit"] += 1
        return WireReply(
            json.dumps(
                dict(
                    id="resp_private_cycle",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=output,
                    usage=dict(
                        input_tokens=100, output_tokens=10, input_tokens_details=dict(cached_tokens=40)
                    ),
                )
            ).encode()
        )

    provider = SimpleNamespace(count=count, submit=submit)
    response = run(
        ModelGateway(tj, lambda _: provider),
        turn,
        profile,
        limits,
        prepared_phase=phase,
        request_snapshot=snapshot,
        manifest_snapshot=manifest,
    )
    assert response["output"] == output
    if structured:
        from podgen.runtime.evaluation.provider_evidence import register_structured

        saved_receipt = history.of_kind("provider_response")[-1]
        decision = register_structured(journal, root, saved_receipt, [request], [call], 0, history)
        assert register_structured(journal, root, saved_receipt, [request], [call], 0, history) == decision
        call = dict(call, decision_entry_id=str(decision))

    class Corpus:
        def execute(self, name, arguments, **options):
            calls["tool"] += 1
            return dict(text="SYNTHETIC complete result", read_id=options["read_id"])

    corpus = Corpus()
    contract = dict(definitions=[dict(name=call["name"], parameters=dict(type="object"))])
    result = _execute(journal, root, corpus, contract, call, 0, 0, lambda: None, history)
    fresh = EvaluationJournal(repo, scope[0])
    restored = fresh.invocation(root["run_id"], "draft")
    replayed = _execute(
        fresh, restored, corpus, contract, call, 0, 0, lambda: None, EventHistory(fresh, restored)
    )
    assert replayed == result and calls == dict(count=1, submit=1, tool=1)
    from podgen.runtime.evaluation.model_turns import rows

    assert rows(fresh, restored)[0]["status"] == "response_completed"
    payload = dict(
        contract="writing-evaluation-output-v1",
        run_id=str(root["run_id"]),
        stage="draft",
        invocation_id=str(root["invocation_id"]),
        request_ref=RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"]).model_dump(
            mode="json"
        ),
        output=dict(text="SYNTHETIC final story"),
    )
    reference = fresh.record(payload, record_id=uuid5(root["invocation_id"], "output"))
    saved = fresh.invocation(root["run_id"], "draft")
    assert saved["status"] == "completed" and fresh.output(saved) == payload
    assert fresh.transition(restored, "completed", output_ref=reference)["status"] == "completed"
    with repo.database.read_transaction() as tx:
        for table in (
            "evaluation_model_invocations",
            "evaluation_model_turns",
            "evaluation_invocation_events",
            "execution_provider_calls",
            "execution_tool_results",
        ):
            assert (
                query(tx, f"SELECT count(*) FROM {table} WHERE project_id=:p", p=scope[0]).scalar_one() == 0
            )
        head = one(
            tx,
            "SELECT * FROM execution_heads WHERE project_id=:p AND run_id=:r",
            p=scope[0],
            r=root["run_id"],
        )
        assert head["accounting"] == dict(
            count_calls=1,
            uncertain_calls=0,
            generation_calls=1,
            input_tokens=100,
            output_tokens=10,
            output_reserved=0,
            cached_input_tokens=40,
        )


def test_legacy_literal_execution_locator_is_never_decoded(repo, scope):
    journal = EvaluationJournal(repo, scope[0])
    payload = {"literal": {"$podgen_execution_ref": {"old": "SYNTHETIC literal data"}}}
    ref = journal.record(payload, kind="user_input")
    assert journal.read(ref) == payload


@pytest.mark.parametrize("turn_index", [0, 3])
def test_research_count_reuses_exact_native_receipt_and_cold_blob_binding(repo, scope, turn_index):
    from podgen.domain.contracts.common import BlobRef
    from podgen.runtime.evaluation.model_turns import TurnJournal
    from podgen.runtime.models.research_context import ResearchContext
    from podgen.runtime.models.research_history import ResearchHistory

    journal, root, _, _, _, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    profile.provider = "openai"
    calls = []

    def count(body):
        calls.append("count")
        return WireReply(b'{"input_tokens":100}')

    def submit(body, **options):
        calls.append("submit")
        return WireReply(
            json.dumps(
                dict(
                    id="resp_exact_cycle",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        )

    provider = SimpleNamespace(count=count, submit=submit)
    history = ResearchHistory(journal, root)
    context = ResearchContext(
        ModelGateway(journal, lambda _: provider), root, profile, snapshot[0], history, lambda: None
    )
    requested, checkpoint = context.request(snapshot[0], turn_index, turn_index)
    assert requested == snapshot[0] and not checkpoint
    receipt = context.receipt(snapshot[0])
    tj = TurnJournal(journal, root, turn_index, history=history)
    turn, phase, request_snapshot = tj.stage_prepare(
        snapshot[0], original_record=context.original_record, pending_events=context.pending_events
    )
    context.pending_events = []
    response = run(
        ModelGateway(tj, lambda _: provider),
        turn,
        profile,
        limits,
        prepared_phase=phase,
        request_snapshot=request_snapshot,
        manifest_snapshot=manifest,
        count_receipt=receipt,
    )
    assert response["id"] == "resp_exact_cycle" and calls == ["count", "submit"]
    from podgen.runtime.evaluation.model_turns import rows

    assert [row["turn_index"] for row in rows(journal, root)] == [turn_index]
    fresh = EvaluationJournal(repo, scope[0])
    record = fresh.read(receipt.reference)
    native = BlobRef.model_validate(record["data"]["native_request_blob"])
    assert json.loads(fresh.read_blob(receipt.reference, native))["model"] == snapshot[0]["model"]
    from podgen.runtime.models.count_receipts import ExactCount

    assert ExactCount(receipt.reference, receipt.identity, 100).validate(fresh, snapshot[0], profile) == 100


def test_usage_overflow_keeps_raw_receipt_and_blocks_same_process_resume(repo, scope):
    from dataclasses import replace
    from uuid import uuid4

    from podgen.domain.contracts.common import BlobRef
    from podgen.persistence.private_journal.contracts import ChargeChange, Checkpoint, HeadChange
    from podgen.persistence.private_journal.writer import commit
    from podgen.runtime.evaluation.contracts import RunFailure

    journal, root, tj, _, _, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    maximum = 2**63 - 1
    bound = journal.execution.sets[root["run_id"]]
    prior = Checkpoint(
        run_id=root["run_id"],
        attempt_id=root["invocation_id"],
        operation_id=uuid4(),
        kind="observation",
        input_set_id=bound["set_id"],
        expected_revision=1,
        data=dict(origin="SYNTHETIC prior paid usage"),
        head=HeadChange(phase="prepared", accounting_delta=dict(input_tokens=maximum - 200)),
    )
    prior = replace(
        prior,
        charges=(ChargeChange(prior.operation_id, 0, {}, dict(input_tokens=maximum - 200), prior.entry_id),),
    )
    commit(repo, scope[0], prior)
    journal.execution.invalidate()
    turn, phase, request_snapshot = tj.stage_prepare(snapshot[0])
    sent = []
    raw = None

    def submit(body, **options):
        nonlocal raw
        sent.append(body)
        raw = json.dumps(
            dict(
                id="resp_overflow",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[],
                usage=dict(input_tokens=300, output_tokens=10),
            )
        ).encode()
        return WireReply(raw)

    provider = SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'), submit=submit)
    gateway = ModelGateway(tj, lambda _: provider)
    limits.max_total_input_tokens = maximum
    options = dict(
        limits=limits,
        totals=dict(input=maximum - 200, output=0),
        guard=lambda: None,
        full_response=True,
        request_snapshot=request_snapshot,
        manifest_snapshot=manifest,
    )
    with pytest.raises(RunFailure, match="run_token_usage_overflow"):
        gateway.invoke_once(turn, profile, prepared_phase=phase, **options)
    cached, _, _ = tj.stage_prepare(snapshot[0])
    assert cached["status"] == "validation_failed"
    with pytest.raises(RunFailure, match="requires_new_attempt"):
        gateway.invoke_once(cached, profile, **options)
    assert len(sent) == 1
    receipt = next(row for row in journal.events(root) if row["event_kind"] == "provider_response")
    reference = RuntimeRef(record_kind="tool_execution", record_id=receipt["record_id"])
    record = journal.read(reference)
    assert journal.read_blob(reference, BlobRef.model_validate(record["response_blob"])) == raw
