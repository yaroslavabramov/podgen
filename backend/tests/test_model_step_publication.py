"""Real SQL crash/validation boundaries with a local provider double."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure, WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.models.contracts import ProviderFault, WireReply
from podgen.runtime.models.gateway import ModelGateway


def setup(repo, scope, *, context=None):
    journal = EvaluationJournal(repo, scope[0])
    profile_ref = create_profile(repo, scope[0], model="gpt-6.1-sol", context_limit=100000).ref
    author = journal.record(dict(contract="synthetic-input/1"), kind="user_input")
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        case_id="phase-test",
        author_ref=author,
        writer_profile_ref=profile_ref,
        reviewer_profile_ref=profile_ref,
    )
    manifest = dict(command=command_payload(command))
    journal.create_run(command, manifest)
    body = dict(
        model="synthetic-model",
        metadata={},
        instructions="Synthetic task",
        input=[],
        text=dict(format=dict(schema={})),
    )
    root = journal.prepare(
        command.run_id, "draft", body, dict(context or {}, profile_ref=profile_ref.model_dump(mode="json"))
    )
    history = model_turns.EventHistory(journal, root)
    turn_journal = model_turns.TurnJournal(journal, root, 0, history=history)
    turn, phase, snapshot = turn_journal.stage_prepare(
        body,
        pending_events=[
            ("source_context_plan", dict(turn_index=0, conversation_hash="synthetic", checkpoint=False))
        ],
    )
    profile = SimpleNamespace(
        context_limit=100000, output_limit=1000, timeout_policy={}, generation_parameters={}
    )
    limits = SimpleNamespace(max_total_input_tokens=1000000, max_total_output_tokens=1000000)
    return journal, root, turn_journal, turn, phase, snapshot, manifest, profile, limits, history


def run(gateway, turn, profile, limits, **options):
    return gateway.invoke_once(
        turn,
        profile,
        limits=limits,
        totals=dict(input=0, output=0),
        guard=lambda: None,
        full_response=True,
        **options,
    )


@pytest.mark.parametrize("failure", ["receipt", "provider_contract", "effective_limits", "provider_factory"])
def test_preflight_failure_preserves_staged_request_and_plan(repo, scope, monkeypatch, failure):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    receipt = None
    if failure == "receipt":
        receipt = SimpleNamespace(validate=lambda *args: (_ for _ in ()).throw(Conflict("bad_count")))
    elif failure == "provider_contract":
        monkeypatch.setattr(
            "podgen.runtime.models.provider_contracts.validate",
            lambda *args: (_ for _ in ()).throw(ContractError("bad_contract")),
        )
    elif failure == "effective_limits":
        monkeypatch.setattr(
            "podgen.runtime.evaluation.input_budget_recovery.effective_limits",
            lambda *args, **kwargs: (_ for _ in ()).throw(RunFailure("bad_limits")),
        )

    def factory(_):
        if failure == "provider_factory":
            raise ProviderFault("missing_openai_api_key")
        pytest.fail("provider must not be constructed")

    gateway = ModelGateway(tj, factory)
    with pytest.raises((ContractError, RunFailure)):
        run(
            gateway,
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
            count_receipt=receipt,
        )
    assert model_turns.rows(journal, root)[0]["status"] == (
        "failed" if failure == "provider_factory" else "validation_failed"
    )
    assert tj.request(turn) == snapshot[0]
    assert [row["event_kind"] for row in journal.events(root)] == [
        "source_context_plan",
        "provider_error" if failure == "provider_factory" else "failure",
    ]


def test_rollback_does_not_publish_state_events_or_in_memory_history(repo, scope):
    journal, root, tj, turn, phase, _, _, _, _, history = setup(repo, scope)
    phase.commit()
    phase = tj.publication(turn)
    ref = phase.event("submit_intent", data=dict(client_request_id=str(turn["invocation_id"])))
    phase.transition("submitting")
    phase.writes.append(lambda tx: (_ for _ in ()).throw(RuntimeError("crash before commit")))
    with pytest.raises(RuntimeError, match="crash before commit"):
        phase.commit()
    assert model_turns.rows(journal, root)[0]["status"] == "prepared"
    assert [row["event_kind"] for row in journal.events(root)] == ["source_context_plan"]
    assert history.of_kind("submit_intent") == []
    with pytest.raises(AccessDenied):
        journal.read(ref)


@pytest.mark.parametrize("field", ["count", "input_tokens", "output_tokens"])
def test_oversized_integer_retains_raw_receipt_and_never_resubmits(repo, scope, field):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    sent = []
    count_raw = json.dumps(dict(input_tokens=2**70 if field == "count" else 100)).encode()

    def submit(body, **kwargs):
        sent.append(body)
        usage = dict(input_tokens=100, output_tokens=10)
        usage[field] = 2**70
        return WireReply(
            json.dumps(
                dict(
                    id="resp_synthetic",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[],
                    usage=usage,
                )
            ).encode()
        )

    provider = SimpleNamespace(count=lambda body: WireReply(count_raw), submit=submit)
    gateway = ModelGateway(tj, lambda _: provider)
    with pytest.raises(RunFailure, match="invalid_input_token_count|provider_invalid_usage"):
        run(
            gateway,
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )
    events = journal.events(root)
    receipt = next(
        row
        for row in events
        if row["event_kind"] == ("count_response" if field == "count" else "provider_response")
    )
    ref = RuntimeRef(record_kind="tool_execution", record_id=receipt["record_id"])
    raw = journal.read_blob(ref, BlobRef.model_validate(journal.read(ref)["response_blob"]))
    assert 2**70 in json.loads(raw).get("usage", json.loads(raw)).values()
    current = dict(turn, **model_turns.rows(journal, root)[0])
    with pytest.raises(RunFailure, match="requires_new_attempt"):
        run(gateway, current, profile, limits)
    assert len(sent) == (0 if field == "count" else 1)


def test_crash_after_submit_commit_resumes_unknown_without_sending_again(repo, scope):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    sent = []

    def submit(body, **kwargs):
        sent.append(body)
        raise RuntimeError("lost acknowledgement")

    provider = SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'), submit=submit)
    gateway = ModelGateway(tj, lambda _: provider)
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        run(
            gateway,
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )
    current = dict(turn, **model_turns.rows(journal, root)[0])
    assert current["status"] == "submitting"
    with pytest.raises(RunFailure, match="submission_acknowledgement_unknown"):
        run(gateway, current, profile, limits)
    assert len(sent) == 1
    assert model_turns.rows(journal, root)[0]["status"] == "outcome_unknown"


@pytest.mark.parametrize("document_count", [1, 129])
@pytest.mark.parametrize("history_length", [0, 128])
def test_ordinary_counted_turn_uses_prepared_inputs_without_graph_or_history_rereads(
    repo, scope, producer, monkeypatch, document_count, history_length
):
    from collections import Counter

    from sqlalchemy import event

    from podgen.persistence import runtime_publication
    from podgen.persistence.batch_insert import insert_rows
    from podgen.runtime.evaluation.turn_ledger import TurnLedger
    from podgen.runtime.models.research_context import ResearchContext
    from podgen.runtime.models.research_history import ResearchHistory
    from tests.factories import Factory

    factory = Factory(repo, *scope, producer)
    policy = factory.make("Policy", dict(policy_kind="data", rules=dict(synthetic=True)))
    factory.policies = dict(data=policy.ref.model_dump(mode="json"))
    documents = [
        factory.source(f"SYNTHETIC complete document {index}")[0].ref for index in range(document_count)
    ]
    journal, root, _, _, _, snapshot, manifest, profile, limits, _ = setup(
        repo, scope, context=dict(source_documents=[ref.model_dump(mode="json") for ref in documents])
    )
    for offset in range(0, history_length, 64):
        phase = journal.publication(root)
        old_turns = []
        for index in range(offset, min(offset + 64, history_length)):
            phase.event("submit_intent", data=dict(source_turn_index=index, client_request_id=str(uuid4())))
            old_turns.append(
                dict(
                    project_id=scope[0],
                    root_invocation_id=root["invocation_id"],
                    turn_index=index,
                    invocation_id=uuid4(),
                    status="response_completed",
                    request_record_id=root["request_record_id"],
                    provider_response_id=f"resp_old_{index}",
                    input_tokens=100,
                    output_tokens=10,
                )
            )
        phase.writes.append(lambda tx, rows=old_turns: insert_rows(tx, "evaluation_model_turns", rows))
        phase.commit()
    profile.provider = "openai"
    body = snapshot[0]
    history = ResearchHistory(journal, root)
    ledger = TurnLedger(journal, root, profile.output_limit, history)
    counters, validations = Counter(), []
    original = runtime_publication._validate_inputs

    def validate(*args):
        validations.append(1)
        return original(*args)

    def capture(conn, cursor, sql, parameters, context, many):
        assert not many
        counters[sql.lstrip().split()[0]] += 1
        if "SELECT * FROM evaluation_model_turns" in sql:
            counters["history"] += 1

    provider = SimpleNamespace(
        count=lambda body: WireReply(b'{"input_tokens":100}'),
        submit=lambda body, **kwargs: WireReply(
            json.dumps(
                dict(
                    id="resp_synthetic",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        ),
    )
    gateway = ModelGateway(journal, lambda _: provider)
    context = ResearchContext(gateway, root, profile, body, history, lambda: None)
    monkeypatch.setattr(runtime_publication, "_validate_inputs", validate)
    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        body, _ = context.request(body, history_length, history_length)
        tj = model_turns.TurnJournal(journal, root, history_length, history=history, ledger=ledger)
        turn, phase, snapshot = tj.stage_prepare(body, pending_events=context.pending_events)
        physical = ModelGateway(tj, lambda _: provider)
        run(
            physical,
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
            count_receipt=context.receipt(body),
        )
        phase = journal.publication(root)
        phase.event("source_tool_call", data=dict(call_index=0, call_id="synthetic", name="probe"))
        phase.commit()
        phase = journal.publication(root)
        phase.event(
            "source_tool_result", data=dict(call_index=0, call_id="synthetic", name="probe", result={})
        )
        phase.commit()
        assert ledger.usage() == dict(
            input=100 * (history_length + 1),
            output=10 * (history_length + 1),
            known_output=10 * (history_length + 1),
            unknown=False,
        )
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert len(validations) == 0
    assert counters["history"] == 1  # One exact turn lookup; ledger never scans history again.
    # Four state projections plus five project serialization fences; none expands inputs.
    assert counters["UPDATE"] == 9
    assert counters["INSERT"] <= 25  # explicit record/blob/control edges plus event/turn projections
    assert counters["SELECT"] < 120 + 32 * ((document_count + 63) // 64)
    assert counters["WITH"] <= 24  # Acquisition metadata is loaded per union, never per document.
    print(
        "ordinary turn growth:",
        document_count,
        "documents,",
        history_length,
        "old turns:",
        dict(counters),
        "union validations:",
        len(validations),
    )


@pytest.mark.parametrize("event_first", [False, True])
def test_provider_id_does_not_overwrite_staged_status(repo, scope, event_first):
    journal, root, tj, turn, phase, _, _, _, _, _ = setup(repo, scope)
    phase.commit()
    phase = tj.publication(turn)
    if event_first:
        phase.event(
            "provider_response", reply=WireReply(b"{}"), data=dict(provider_response_id="resp_synthetic")
        )
    phase.transition("response_completed", input_tokens=100, output_tokens=10)
    if not event_first:
        phase.event(
            "provider_response", reply=WireReply(b"{}"), data=dict(provider_response_id="resp_synthetic")
        )
    row = phase.commit()
    assert row["status"] == "response_completed"
    assert row["provider_response_id"] == "resp_synthetic"
    assert model_turns.rows(journal, root)[0]["status"] == "response_completed"


def test_dynamic_admissions_are_technical_inputs_of_new_request_and_count_intent(repo, scope):
    journal, root, tj, turn, _, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    admission = journal.record(dict(contract="synthetic-runtime-admission/1"))
    body = snapshot[0]
    turn, phase, snapshot = tj.stage_prepare(body, admission_refs=[admission])

    class Guard:
        def __call__(self):
            pass

        def admission_refs(self):
            return [admission]

    guard = Guard()
    provider = SimpleNamespace(
        count=lambda body: WireReply(b'{"input_tokens":100}'),
        submit=lambda body, **kwargs: WireReply(
            json.dumps(
                dict(
                    id="resp_synthetic",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        ),
    )
    gateway = ModelGateway(tj, lambda _: provider)
    gateway.invoke_once(
        turn,
        profile,
        limits=limits,
        totals=dict(input=0, output=0),
        guard=guard,
        full_response=True,
        prepared_phase=phase,
        request_snapshot=snapshot,
        manifest_snapshot=manifest,
    )
    record = journal.read(RuntimeRef(record_kind="tool_execution", record_id=turn["request_record_id"]))
    assert record["context"]["runtime_admission_refs"] == [admission.model_dump(mode="json")]
    assert tj.request(turn) == body
    original = journal.read(RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"]))
    assert "runtime_admission_refs" not in original["context"]
    intent = next(row for row in journal.events(root) if row["event_kind"] == "count_intent")
    saved = journal.read(RuntimeRef(record_kind="tool_execution", record_id=intent["record_id"]))
    assert saved["data"]["runtime_admission_refs"] == [admission.model_dump(mode="json")]


def test_action_guard_sees_new_sha_classification_in_same_publication_snapshot(repo, scope):
    import io

    from podgen.persistence.read_authorization import authorized_read
    from podgen.persistence.repository import Repository

    journal, root, _, _, _, _, _, _, _, _ = setup(repo, scope)
    project, _ = scope
    body = b"SYNTHETIC same SHA source and receipt"
    blob = repo.save_blob(project, io.BytesIO(body), "text/plain")
    source = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, source, dict(blob=blob.model_dump(mode="json")))
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    assert generation.get_runtime(project, source)
    phase = journal.publication(root)
    assert phase.blob(body, "text/plain") == blob
    ref = phase.record(dict(blob=blob.model_dump(mode="json")))

    class Guard:
        def __call__(self):
            pytest.fail("must use publication snapshot")

        def authorize_publication(self, tx):
            with authorized_read(generation, tx):
                generation.authorize_runtime(project, source)

    with pytest.raises(AccessDenied):
        phase.commit(guard=Guard())
    with pytest.raises(AccessDenied):
        journal.read(ref)
    # The denial rolled back classification as well as the proposed consumer.
    assert generation.get_runtime(project, source)


def test_root_usage_overflow_does_not_roll_back_received_response(repo, scope):
    from podgen.runtime.evaluation.turn_ledger import TurnLedger
    from podgen.runtime.models.tool_gateway import _account

    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, history = setup(repo, scope)
    ledger = TurnLedger(journal, root, profile.output_limit, history)
    tj.ledger = ledger
    received = []

    def submit(body, **kwargs):
        amount = 1 if not received else 2**63 - 1
        raw = json.dumps(
            dict(
                id=f"resp_{len(received)}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[],
                usage=dict(input_tokens=amount, output_tokens=10),
            )
        ).encode()
        received.append(raw)
        return WireReply(raw)

    provider = SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'), submit=submit)
    gateway = ModelGateway(tj, lambda _: provider)
    run(
        gateway,
        turn,
        profile,
        limits,
        prepared_phase=phase,
        request_snapshot=snapshot,
        manifest_snapshot=manifest,
    )
    second = model_turns.TurnJournal(journal, root, 1, history=history, ledger=ledger)
    row, phase, snapshot = second.stage_prepare(snapshot[0])
    run(
        ModelGateway(second, lambda _: provider),
        row,
        profile,
        limits,
        prepared_phase=phase,
        request_snapshot=snapshot,
        manifest_snapshot=manifest,
    )
    with pytest.raises(RunFailure, match="run_token_usage_overflow"):
        _account(journal, root, profile, history, ledger=ledger)
    assert journal.invocation(root["run_id"], root["stage"])["status"] == "validation_failed"
    receipts = [entry for entry in journal.events(root) if entry["event_kind"] == "provider_response"]
    ref = RuntimeRef(record_kind="tool_execution", record_id=receipts[-1]["record_id"])
    assert journal.read_blob(ref, BlobRef.model_validate(journal.read(ref)["response_blob"])) == received[-1]
    assert len(received) == 2
