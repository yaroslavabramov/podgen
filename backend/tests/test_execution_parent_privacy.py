"""PostgreSQL regressions for persisted execution parents, independent of gateway integration."""

import hashlib
import io
import json
from copy import deepcopy
from uuid import uuid4, uuid5

import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.database import one, query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.execution.inputs import bind, refresh
from podgen.persistence.execution.live import authorize
from podgen.persistence.execution.objects import OwnedBlob
from podgen.persistence.execution.store import append
from podgen.persistence.read_authorization import authorized_read
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.factories import Factory


class SavedExchange:
    def __init__(self, repo, project, run_id, bound):
        self.repo, self.project, self.run_id, self.bound = repo, project, run_id, bound
        self.journal = EvaluationJournal(repo, project)

    def owned(self, body):
        blob = self.repo.save_blob(self.project, io.BytesIO(body), "application/json")
        return OwnedBlob(blob, body)

    def publish(self, payload, owned=(), *, record_id=None):
        ref = RuntimeRef(record_kind="tool_execution", record_id=record_id or uuid4())

        def write(tx):
            append(
                self.repo,
                tx,
                self.project,
                self.run_id,
                self.bound["set_id"],
                [(ref, payload, False)],
                owned,
            )
            if payload.get("contract") == "model-invocation-event-v1":
                query(
                    tx,
                    """INSERT INTO evaluation_invocation_events
                    (project_id,invocation_id,event_id,record_id,event_kind,source_turn_index)
                    VALUES(:p,:i,:event,:record,:kind,:turn)""",
                    p=self.project,
                    i=self.invocation_id,
                    event=uuid4(),
                    record=ref.record_id,
                    kind=payload["event_kind"],
                    turn=payload.get("data", {}).get("source_turn_index"),
                )

        archive_write(self.repo, self.project, write)
        return ref

    def event(self, kind, data=None, *, owned=None, record_id=None):
        payload = dict(
            contract="model-invocation-event-v1",
            invocation_id=str(self.invocation_id),
            event_kind=kind,
            request_ref=self.root_request.model_dump(mode="json"),
            data=dict(
                data or {},
                source_turn_index=0,
                source_turn_id=str(self.physical_id),
                turn_request_ref=self.request.model_dump(mode="json"),
            ),
        )
        if owned is not None:
            payload.update(
                response_blob=owned.reference.model_dump(mode="json"),
                provider_http_status=200,
            )
        return self.publish(payload, [owned] if owned is not None else [], record_id=record_id)

    def provider_response(self, *, owned=None, record_id=None):
        raw_arguments = '{"source_id":"S1","start":0,"end":20}'
        call = dict(
            type="function_call",
            call_id="call_synthetic_read",
            name="read_source",
            arguments=raw_arguments,
            status="completed",
        )
        if owned is None:
            owned = self.owned(
                json.dumps(
                    dict(
                        id="resp_synthetic",
                        model="synthetic-model",
                        metadata=dict(podgen_invocation_id=str(self.physical_id)),
                        status="completed",
                        output=[call],
                        usage=dict(input_tokens=100, output_tokens=10),
                    )
                ).encode()
            )
        response = self.event("provider_response", owned=owned, record_id=record_id)
        # Project the actual saved provider call, rather than inventing a pending parent.
        saved_call = json.loads(owned.body)["output"][0]
        with self.repo.database.transaction() as tx:
            query(
                tx,
                """INSERT INTO execution_provider_calls
                (project_id,run_id,response_record_id,physical_invocation_id,request_record_id,
                 call_id,arguments_hash,tool_name)
                VALUES(:p,:r,:response,:physical,:request,:call,:hash,:name)""",
                p=self.project,
                r=self.run_id,
                response=response.record_id,
                physical=self.physical_id,
                request=self.request.record_id,
                call=saved_call["call_id"],
                hash=hashlib.sha256(saved_call["arguments"].encode()).hexdigest(),
                name=saved_call["name"],
            )
        return (
            response,
            owned.reference,
            dict(
                turn_index=0,
                call_index=0,
                read_id="E1",
                call_id=saved_call["call_id"],
                name=saved_call["name"],
                raw_arguments=saved_call["arguments"],
                arguments=json.loads(saved_call["arguments"]),
                arguments_hash=hashlib.sha256(saved_call["arguments"].encode()).hexdigest(),
                provider_response_ref=response.model_dump(mode="json"),
                result=dict(text="SYNTHETIC complete saved tool result"),
            ),
        )

    def refresh_valid_inputs(self):
        refresh(self.repo, self.project, self.bound["set_id"])
        with self.repo.database.read_transaction() as tx, authorized_read(self.repo, tx):
            assert authorize(self.repo, tx, self.project, self.bound["set_id"], action=True)
            # The independent request-body alias is intentionally outside the input set.
            assert (
                one(
                    tx,
                    """SELECT 1 FROM execution_input_resources
                WHERE project_id=:p AND set_id=:s AND resource_kind='blob'
                  AND binding->>'sha256'=:sha""",
                    p=self.project,
                    s=self.bound["set_id"],
                    sha=self.request_blob.sha256,
                )
                is None
            )

    def withdraw_request_alias(self):
        event = DependencyLedger(self.repo).invalidate(
            self.project, self.alias, reason="SYNTHETIC request SHA alias withdrawal", category="access"
        )
        self.refresh_valid_inputs()
        with self.repo.database.transaction() as tx:
            assert one(
                tx,
                "SELECT 1 FROM blob_access_revocations WHERE project_id=:p AND sha256=:sha",
                p=self.project,
                sha=self.request_blob.sha256,
            )
        return event


@pytest.fixture
def exchange(repo, scope, producer):
    project, episode = scope
    factory = Factory(repo, project, episode, producer)
    policy = factory.make("Policy", dict(policy_kind="data", rules=dict(synthetic=True)))
    factory.policies = dict(data=policy.ref.model_dump(mode="json"))
    source, _ = factory.source("SYNTHETIC actual model input, unrelated to request bytes")
    request_body = b'{"model":"synthetic-model","input":"SYNTHETIC physical request"}'
    alias, alias_blob = factory.source(request_body.decode())

    run_id, invocation = uuid4(), uuid4()
    manifest = RuntimeRef(record_kind="context_assembly", record_id=uuid4())
    repo.append_runtime(project, manifest, dict(contract="synthetic-parent-privacy-manifest/1"))
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_writing_runs
            (project_id,run_id,actor_id,input_fingerprint,manifest_id,status)
            VALUES(:p,:r,:actor,:hash,:manifest,'prepared')""",
            p=project,
            r=run_id,
            actor=repo.actor_id,
            hash=digest(dict(test="execution-parent-privacy")),
            manifest=manifest.record_id,
        )
    bound = bind(repo, project, run_id, [dict(purpose="generation", ref=source.ref.model_dump(mode="json"))])
    saved = SavedExchange(repo, project, run_id, bound)
    saved.source = source.ref
    saved.invocation_id = invocation
    saved.physical_id = uuid5(invocation, "source-turn:0")
    saved.alias = alias.ref
    root_body = saved.owned(b'{"model":"synthetic-model","input":"SYNTHETIC root request"}')
    saved.root_request = saved.publish(
        dict(
            contract="model-request-v1",
            invocation_id=str(invocation),
            run_id=str(run_id),
            stage="draft",
            context={},
            request_blob=root_body.reference.model_dump(mode="json"),
        ),
        [root_body],
        record_id=uuid5(invocation, "request"),
    )
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_model_invocations
            (project_id,run_id,invocation_id,stage,status,request_record_id)
            VALUES(:p,:r,:i,'draft','tool_running',:request)""",
            p=project,
            r=run_id,
            i=invocation,
            request=saved.root_request.record_id,
        )
    physical = saved.owned(request_body)
    saved.request_blob = physical.reference
    assert alias_blob.sha256 == saved.request_blob.sha256
    assert alias_blob.storage_key != saved.request_blob.storage_key
    saved.request = saved.publish(
        dict(
            contract="model-request-v1",
            invocation_id=str(saved.physical_id),
            run_id=str(run_id),
            stage="draft",
            context=dict(source_tool_turn=dict(root_invocation_id=str(invocation), turn_index=0)),
            request_blob=physical.reference.model_dump(mode="json"),
        ),
        [physical],
        record_id=uuid5(saved.physical_id, "request"),
    )
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_model_turns
            (project_id,root_invocation_id,turn_index,invocation_id,status,request_record_id)
            VALUES(:p,:root,0,:physical,'submitting',:request)""",
            p=project,
            root=invocation,
            physical=saved.physical_id,
            request=saved.request.record_id,
        )
    saved.refresh_valid_inputs()
    return saved


def test_request_alias_withdrawal_before_receipt_rejects_append_with_current_valid_inputs(exchange):
    exchange.withdraw_request_alias()
    receipt_id = uuid4()
    with pytest.raises(AccessDenied):
        exchange.provider_response(record_id=receipt_id)
    with exchange.repo.database.transaction() as tx:
        assert (
            one(
                tx,
                "SELECT 1 FROM runtime_records WHERE project_id=:p AND record_id=:id",
                p=exchange.project,
                id=receipt_id,
            )
            is None
        )


def test_later_alias_withdrawal_closes_receipt_without_own_blobs_and_warm_reads(exchange):
    response, _, data = exchange.provider_response()
    receipt = exchange.event("source_tool_result", data)
    with exchange.repo.database.transaction() as tx:
        assert (
            one(
                tx,
                "SELECT 1 FROM runtime_blob_refs WHERE project_id=:p AND record_id=:id",
                p=exchange.project,
                id=receipt.record_id,
            )
            is None
        )
    with exchange.journal.runtime_graph():
        assert exchange.journal.read(receipt)["data"]["result"] == data["result"]
        exchange.withdraw_request_alias()
        for ref in (exchange.request, response, receipt):
            with pytest.raises(AccessDenied):
                exchange.journal.read(ref)
    with exchange.repo.database.transaction() as tx:
        assert one(
            tx,
            "SELECT 1 FROM execution_record_withdrawals WHERE project_id=:p AND record_id=:id",
            p=exchange.project,
            id=receipt.record_id,
        )


def test_privacy_parent_does_not_grant_its_blob_through_child(exchange):
    _, response_blob, data = exchange.provider_response()
    receipt = exchange.event("source_tool_result", data)
    assert exchange.journal.read(receipt)["data"]["result"] == data["result"]
    assert exchange.journal.read_blob(exchange.request, exchange.request_blob)
    for blob in (exchange.request_blob, response_blob):
        with pytest.raises(AccessDenied):
            exchange.journal.read_blob(receipt, blob)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_generation_only_source_withdrawal_closes_receipt_bytes_and_independent_alias(exchange, warm):
    assert exchange.bound["roots"] == [
        dict(purpose="generation", ref=exchange.source.model_dump(mode="json"))
    ]
    receipt, receipt_blob, _ = exchange.provider_response()
    raw = exchange.journal.read_blob(receipt, receipt_blob)
    alias_blob = exchange.repo.save_blob(exchange.project, io.BytesIO(raw), "text/plain")
    assert alias_blob.sha256 == receipt_blob.sha256
    assert alias_blob.storage_key != receipt_blob.storage_key
    alias = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    exchange.repo.append_runtime(
        exchange.project, alias, dict(blob=alias_blob.model_dump(mode="json")), audience="evaluator"
    )
    with exchange.repo.database.transaction() as tx:
        projected_source = one(
            tx,
            """SELECT binding FROM execution_input_resources
            WHERE project_id=:p AND set_id=:s AND purpose='evaluation'
              AND resource_kind='artifact' AND owner_project_id=:p AND identity=:source
              AND read_required AND privacy_required""",
            p=exchange.project,
            s=exchange.bound["set_id"],
            source=str(exchange.source.revision_id),
        )
        assert projected_source is not None
        assert projected_source["binding"] == exchange.source.model_dump(mode="json")

    with exchange.journal.runtime_graph():
        if warm:
            assert exchange.journal.read(receipt)["event_kind"] == "provider_response"
            assert exchange.journal.read_blob(receipt, receipt_blob) == raw
            assert exchange.repo.read_runtime_blob(exchange.project, alias, alias_blob) == raw
        DependencyLedger(exchange.repo).invalidate(
            exchange.project,
            exchange.source,
            reason="SYNTHETIC generation source withdrawal",
            category="access",
        )
        with exchange.repo.database.transaction() as tx:
            assert (
                one(
                    tx,
                    "SELECT role FROM project_members WHERE project_id=:p AND actor_id=:actor",
                    p=exchange.project,
                    actor=exchange.repo.actor_id,
                )["role"]
                == "owner"
            )
            # Verify real propagation, independently of stale-expansion rejection.
            assert one(
                tx,
                "SELECT 1 FROM blob_access_revocations WHERE project_id=:p AND sha256=:sha",
                p=exchange.project,
                sha=receipt_blob.sha256,
            )
        for read, arguments in (
            (exchange.journal.read, (receipt,)),
            (exchange.journal.read_blob, (receipt, receipt_blob)),
            (exchange.repo.get_runtime, (exchange.project, alias)),
            (exchange.repo.read_runtime_blob, (exchange.project, alias, alias_blob)),
        ):
            with pytest.raises(AccessDenied):
                read(*arguments)


def test_same_run_reference_outside_control_path_requires_material_binding(exchange):
    _, _, data = exchange.provider_response()
    exchange.event("source_tool_result", data)  # Positive control uses persisted parents.
    forged = deepcopy(data)
    forged["result"]["unbound_context_ref"] = exchange.request.model_dump(mode="json")
    with pytest.raises(ContractError, match="execution_dependency_not_bound"):
        exchange.event("source_tool_result", forged)


def test_raw_arguments_must_match_projected_hash_from_saved_response(exchange):
    _, _, data = exchange.provider_response()
    data.pop("result")
    exchange.event("source_tool_call", data)
    forged = deepcopy(data)
    forged["raw_arguments"] = '{"source_id":"S2","start":0,"end":20}'
    forged["arguments"] = json.loads(forged["raw_arguments"])
    assert forged["arguments_hash"] != hashlib.sha256(forged["raw_arguments"].encode()).hexdigest()
    with pytest.raises(ContractError, match="execution_control_binding_mismatch"):
        exchange.event("source_tool_call", forged)


def test_equal_token_counts_do_not_make_different_count_identities_interchangeable(exchange):
    native = exchange.owned(b'{"model":"synthetic-model","input":"SYNTHETIC counted request"}')
    configuration = dict(provider="openai", api_family="responses")
    identity = digest(
        dict(
            contract="exact-native-count/1",
            native_request=json.loads(native.body),
            provider=configuration,
        )
    )
    count_data = dict(
        count_contract="exact-native-count/1",
        count_identity=identity,
        native_request_blob=native.reference.model_dump(mode="json"),
        provider_configuration=configuration,
    )
    intent_payload = dict(
        contract="model-invocation-event-v1",
        invocation_id=str(exchange.invocation_id),
        event_kind="source_context_count_intent",
        request_ref=exchange.root_request.model_dump(mode="json"),
        data=count_data,
    )
    intent = exchange.publish(intent_payload, [native])
    receipt = exchange.event(
        "source_context_count",
        dict(count_data, input_tokens=100, count_intent_ref=intent.model_dump(mode="json")),
        owned=exchange.owned(b'{"input_tokens":100}'),
    )
    reuse = dict(count_ref=receipt.model_dump(mode="json"), count_identity=identity, input_tokens=100)
    exchange.event("count_receipt_reused", reuse)
    different_identity = digest(dict(native_request=dict(input="SYNTHETIC different count request")))
    assert different_identity != identity
    with pytest.raises(ContractError, match="execution_control_binding_mismatch"):
        exchange.event("count_receipt_reused", dict(reuse, count_identity=different_identity))
