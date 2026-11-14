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
        provider="openai",
        api_family="responses",
        adapter_version="openai-responses-v1",
        model_revision_or_alias=body["model"],
        context_limit=100000,
        output_limit=1000,
        timeout_policy={},
        generation_parameters={},
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


def test_rollback_does_not_publish_state_events_or_in_memory_history(repo, scope, monkeypatch):
    from podgen.persistence.private_journal import writer

    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, history = setup(repo, scope)
    attempted = []
    original = writer.insert_rows

    def insert_then_crash(tx, table, rows, **options):
        original(tx, table, rows, **options)
        if table == "execution_entries" and rows[0]["checkpoint_kind"] == "provider_receipt":
            attempted.extend(rows)
            raise RuntimeError("crash before commit")

    monkeypatch.setattr(writer, "insert_rows", insert_then_crash)
    sent = []

    def submit(body, **options):
        sent.append(body)
        return WireReply(
            json.dumps(
                dict(
                    id="resp_rollback",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        )

    provider = SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'), submit=submit)
    with pytest.raises(RuntimeError, match="crash before commit"):
        run(
            ModelGateway(tj, lambda _: provider),
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )
    assert len(sent) == 1 and len(attempted) == 1
    assert model_turns.rows(journal, root)[0]["status"] == "submitting"
    assert [row["event_kind"] for row in journal.events(root)] == [
        "source_context_plan",
        "count_intent",
        "count_response",
        "submit_intent",
    ]
    assert len(history.of_kind("submit_intent")) == 1
    assert history.of_kind("provider_response") == []
    view = json.loads(attempted[0]["archive_views"])[0]
    ref = RuntimeRef(record_kind=view["record_kind"], record_id=view["record_id"])
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
    from podgen.runtime.evaluation.turn_ledger import TurnLedger
    from podgen.runtime.models.research_context import ResearchContext
    from podgen.runtime.models.research_history import ResearchHistory
    from podgen.runtime.models.tool_gateway import _execute
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
    profile.provider = "openai"
    body = snapshot[0]
    call = dict(type="function_call", call_id="synthetic", name="request_source", arguments="{}")
    calls = Counter()

    def count(body):
        calls["count"] += 1
        return WireReply(b'{"input_tokens":100}')

    def submit(body, **options):
        calls["submit"] += 1
        return WireReply(
            json.dumps(
                dict(
                    id=f"resp_synthetic_{calls['submit']}",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[call],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        )

    provider = SimpleNamespace(count=count, submit=submit)
    # Seed real private count/submit/receipt operations outside the measured turn.
    # No legacy rows or fabricated completed physical states stand in for history.
    old_history = model_turns.EventHistory(journal, root)
    for index in range(history_length):
        old = model_turns.TurnJournal(journal, root, index, history=old_history)
        row, pending, request = old.stage_prepare(dict(body, instructions=f"Synthetic old task {index}"))
        run(
            ModelGateway(old, lambda _: provider),
            row,
            profile,
            limits,
            prepared_phase=pending,
            request_snapshot=request,
            manifest_snapshot=manifest,
        )
    assert calls["count"] == calls["submit"] == history_length
    history = ResearchHistory(journal, root)
    ledger = TurnLedger(journal, root, profile.output_limit, history)
    counters, validations = Counter(), []

    def validate(*args, **kwargs):
        validations.append(1)
        pytest.fail("private publication reconstructed generic runtime inputs")

    def capture(conn, cursor, sql, parameters, context, many):
        assert not many
        statement = sql.lstrip()
        counters[statement.split()[0]] += 1
        if "SELECT * FROM evaluation_model_turns" in sql:
            assert "turn_index=" in sql
            counters["history"] += 1
        if "data,archive_views FROM execution_entries" in sql:
            assert "step_id=" in sql
        if statement.startswith("INSERT INTO execution_entries "):
            counters["checkpoints"] += 1
        if statement.startswith(
            (
                "INSERT INTO evaluation_model_",
                "INSERT INTO evaluation_invocation_",
                "INSERT INTO execution_provider_calls ",
                "INSERT INTO execution_tool_results ",
                "INSERT INTO runtime_records ",
                "INSERT INTO runtime_runtime_refs ",
                "INSERT INTO runtime_blob_refs ",
            )
        ):
            pytest.fail("private execution duplicated a legacy projection")

    gateway = ModelGateway(journal, lambda _: provider)
    context = ResearchContext(gateway, root, profile, body, history, lambda: None)
    # Pin the exact native identity validation, provider calls and every SQL phase
    # of one complete new turn, including authenticated tool intent and result.
    monkeypatch.setattr(runtime_publication, "_validate_inputs", validate)
    monkeypatch.setattr("podgen.persistence.execution.inputs.replace_expansion", validate)
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
        result = _execute(
            journal,
            root,
            SimpleNamespace(execute=lambda *args, **kwargs: dict(synthetic="complete tool result")),
            dict(definitions=[dict(name=call["name"], parameters=dict(type="object"))]),
            call,
            0,
            history_length,
            lambda: None,
            history,
        )
        assert result == dict(synthetic="complete tool result")
        assert ledger.usage() == dict(
            input=100 * (history_length + 1),
            output=10 * (history_length + 1),
            known_output=10 * (history_length + 1),
            unknown=False,
        )
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert len(validations) == 0
    assert counters["history"] == 0  # Count preparation already owns the current physical descriptor.
    assert counters["checkpoints"] == 5
    assert calls == dict(count=history_length + 1, submit=history_length + 1)
    # SQL bounds cover the complete operation and stay independent of history.
    assert counters["UPDATE"] <= 12
    assert counters["INSERT"] <= 10
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
def test_provider_id_does_not_overwrite_staged_status(repo, scope, monkeypatch, event_first):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    replies = []

    def submit(body, **options):
        return WireReply(
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
        )

    provider = SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'), submit=submit)
    gateway = ModelGateway(tj, lambda _: provider)

    def before_capture(invocation, reply, body, **options):
        replies.append(reply)
        raise RuntimeError("pause after actual submit")

    monkeypatch.setattr(gateway, "_archive_reply", before_capture)
    with pytest.raises(RuntimeError, match="pause after actual submit"):
        run(
            gateway,
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )
    turn = dict(turn, **model_turns.rows(journal, root)[0])
    assert turn["status"] == "submitting" and len(replies) == 1
    phase = tj.publication(turn)
    if event_first:
        phase.event("provider_response", reply=replies[0], data=dict(provider_response_id="resp_synthetic"))
    phase.transition("response_completed", input_tokens=100, output_tokens=10)
    if not event_first:
        phase.event("provider_response", reply=replies[0], data=dict(provider_response_id="resp_synthetic"))
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

    from podgen.persistence.repository import Repository

    journal, root, _, _, _, _, _, _, _, _ = setup(repo, scope)
    project, _ = scope
    body = b"SYNTHETIC same SHA source and receipt"
    blob = repo.save_blob(project, io.BytesIO(body), "text/plain")
    source = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, source, dict(blob=blob.model_dump(mode="json")))
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    assert generation.get_runtime(project, source)
    journal.execution.prepare(root["run_id"], generation=[source])
    phase = journal.publication(root)
    assert phase.blob(body, "text/plain") == blob
    ref = phase.record(dict(blob=blob.model_dump(mode="json")))

    class Guard:
        def __call__(self):
            pytest.fail("must use publication snapshot")

        def prepare_publication(self):
            pass

    with pytest.raises(AccessDenied):
        phase.commit(guard=Guard())
    with pytest.raises(AccessDenied):
        journal.read(ref)
    # The denial rolled back classification as well as the proposed consumer.
    assert generation.get_runtime(project, source)


def test_upload_rechecks_membership_after_preflight_parent_reuse(repo, scope, monkeypatch):
    from podgen.persistence.database import query
    from podgen.persistence.private_journal import writer

    journal, root, *_ = setup(repo, scope)
    before = dict(journal.execution.current_head(root["run_id"]))
    phase = journal.publication(root)
    ref = phase.event("probe", reply=WireReply(b'{"complete":"SYNTHETIC received bytes"}'))
    uploads, parent_reads = [], []
    original_upload, original_parents = writer.upload, writer.control.parents

    def parents(*args, **kwargs):
        parent_reads.append(1)
        return original_parents(*args, **kwargs)

    def upload_then_revoke(repo, project, values):
        assert repo._read_authorization.get() is None
        original_upload(repo, project, values)
        uploads.append(1)
        with repo.database.transaction() as tx:
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:actor",
                p=project,
                actor=repo.actor_id,
            )

    monkeypatch.setattr(writer.control, "parents", parents)
    monkeypatch.setattr(writer, "upload", upload_then_revoke)
    with pytest.raises(AccessDenied):
        phase.commit()
    assert uploads == [1] and parent_reads == [1]
    assert not phase.finished and repo._read_authorization.get() is None
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx,
                "SELECT revision FROM execution_heads WHERE project_id=:p AND run_id=:r",
                p=scope[0],
                r=root["run_id"],
            ).scalar_one()
            == before["revision"]
        )
        assert (
            query(
                tx,
                "SELECT count(*) FROM execution_entries WHERE project_id=:p AND archive_ids @> ARRAY[CAST(:id AS uuid)]",
                p=scope[0],
                id=ref.record_id,
            ).scalar_one()
            == 0
        )


def test_root_usage_overflow_does_not_roll_back_received_response(repo, scope):
    from podgen.runtime.evaluation.turn_ledger import TurnLedger

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
    with pytest.raises(RunFailure, match="run_token_usage_overflow"):
        run(
            ModelGateway(second, lambda _: provider),
            row,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )
    assert journal.invocation(root["run_id"], root["stage"])["status"] == "validation_failed"
    assert ledger.rows[1]["status"] == "validation_failed"
    warm, pending, _ = second.stage_prepare(snapshot[0])
    assert warm["status"] == "validation_failed" and pending is None
    fresh = EvaluationJournal(repo, scope[0])
    restored = fresh.invocation(root["run_id"], root["stage"])
    cold = model_turns.TurnJournal(fresh, restored, 1)
    cold_row, pending, _ = cold.stage_prepare(snapshot[0])
    assert cold_row["status"] == "validation_failed" and pending is None
    for owner, current in ((second, warm), (cold, cold_row)):
        with pytest.raises(RunFailure, match="requires_new_attempt"):
            run(ModelGateway(owner, lambda _: provider), current, profile, limits)
    receipts = [entry for entry in journal.events(root) if entry["event_kind"] == "provider_response"]
    ref = RuntimeRef(record_kind="tool_execution", record_id=receipts[-1]["record_id"])
    assert journal.read_blob(ref, BlobRef.model_validate(journal.read(ref)["response_blob"])) == received[-1]
    assert len(received) == 2
