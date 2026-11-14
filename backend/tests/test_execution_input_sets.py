"""Prepared metadata is not a permission cache; execution publication cannot enter the graph writer."""

import io
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.database import query
from podgen.persistence.execution.inputs import bind, normalize
from podgen.persistence.execution.live import authorize
from podgen.persistence.execution.objects import OwnedBlob
from podgen.persistence.execution.store import append
from podgen.persistence.read_authorization import authorized_read
from tests.test_model_step_publication import setup


def source(repo, project, value=b"SYNTHETIC full source"):
    blob = repo.save_blob(project, io.BytesIO(value), "text/plain")
    ref = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, ref, dict(blob=blob.model_dump(mode="json")))
    return ref, blob


def inputs(repo, scope, *, generation=False):
    journal, root, *_ = setup(repo, scope)
    ref, blob = source(repo, scope[0])
    bound = bind(
        repo,
        scope[0],
        root["run_id"],
        [dict(purpose="generation" if generation else "evaluation", ref=ref.model_dump(mode="json"))],
    )
    return journal, root, bound, ref, blob


def publish(repo, project, root, bound, payload, blobs=()):
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    from podgen.persistence.execution.inputs import _publish

    _publish(
        repo,
        project,
        lambda tx: append(repo, tx, project, root["run_id"], bound["set_id"], [(ref, payload, False)], blobs),
    )
    return ref


@pytest.mark.parametrize(
    "value",
    [
        [],
        [dict(purpose="evaluaton", ref={})],
        [dict(purpose="evaluation", ref=dict(record_id=str(uuid4()), record_kind="invalid"))],
        [
            dict(
                purpose="generation",
                ref=dict(storage_key="x", sha256="0" * 64, byte_length=1, media_type="text/plain"),
                unknown=True,
            )
        ],
    ],
)
def test_roots_are_strict_before_hashing(value):
    with pytest.raises(ContractError):
        normalize(value)


def test_receipt_append_and_read_never_authorize_semantic_graph(repo, scope, monkeypatch):
    journal, root, bound, _, _ = inputs(repo, scope)
    raw = repo.save_blob(scope[0], io.BytesIO(b"SYNTHETIC complete receipt"), "application/json")

    def forbidden(*args, **kwargs):
        pytest.fail("graph machinery entered ordinary journal checkpoint")

    monkeypatch.setattr("podgen.persistence.runtime_records._validate_inputs", forbidden)
    monkeypatch.setattr("podgen.persistence.scope.ScopeAccess.resolve", forbidden)
    monkeypatch.setattr("podgen.persistence.privacy_graph._protected_origins", forbidden)
    payload = dict(response_blob=raw.model_dump(mode="json"), full="Uncut response")
    ref = publish(repo, scope[0], root, bound, payload, [OwnedBlob(raw, b"SYNTHETIC complete receipt")])
    assert journal.read(ref) == payload
    assert journal.read_blob(ref, raw) == b"SYNTHETIC complete receipt"


def test_writer_rejects_unbound_source_and_forged_owned_blob(repo, scope):
    _, root, bound, _, _ = inputs(repo, scope)
    other, blob = source(repo, scope[0], b"SYNTHETIC unrelated input")
    # An unrelated artifact creation would require a refresh; a new immutable
    # runtime does not change the prepared roots or project topology version.
    for value in (other, blob):
        with pytest.raises(ContractError, match="execution_dependency_not_bound"):
            publish(repo, scope[0], root, bound, dict(forged=value.model_dump(mode="json")))


def test_generation_only_root_is_fully_protected_on_evaluator_receipt_read(repo, scope):
    journal, root, bound, _, blob = inputs(repo, scope, generation=True)
    ref = publish(repo, scope[0], root, bound, dict(full="saved answer"))
    with repo.database.transaction() as tx:
        query(
            tx, "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a", p=scope[0], a=repo.actor_id
        )
    with pytest.raises(AccessDenied):
        journal.read(ref)


def test_classification_is_live_without_topology_bump(repo, scope):
    journal, root, bound, _, blob = inputs(repo, scope, generation=True)
    ref = publish(repo, scope[0], root, bound, dict(full="saved answer"))
    journal.record(dict(bytes=blob.model_dump(mode="json")))
    assert journal.read(ref) == dict(full="saved answer")
    with pytest.raises(AccessDenied), repo.database.read_transaction() as tx, authorized_read(repo, tx):
        authorize(repo, tx, scope[0], bound["set_id"], action=True)


def owned_request(repo, scope, root, bound, producer):
    from podgen.persistence.execution.inputs import refresh

    body = b'{"model":"SYNTHETIC private request bytes"}'
    blob = repo.save_blob(scope[0], io.BytesIO(body), "application/json")
    owner = repo.create(
        scope[0],
        "Policy",
        dict(policy_kind="data", rules=dict(blob=blob.model_dump(mode="json"))),
        producer=producer,
        operation_id=uuid4(),
    )
    refresh(repo, scope[0], bound["set_id"])
    invocation = uuid4()
    request = publish(
        repo,
        scope[0],
        root,
        bound,
        dict(
            contract="model-request-v1",
            invocation_id=str(invocation),
            run_id=str(root["run_id"]),
            stage="review",
            context={},
            request_blob=blob.model_dump(mode="json"),
        ),
        [OwnedBlob(blob, body)],
    )
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_model_invocations
            (project_id,run_id,invocation_id,stage,status,request_record_id)
            VALUES(:p,:r,:i,'review','submitting',:request)""",
            p=scope[0],
            r=root["run_id"],
            i=invocation,
            request=request.record_id,
        )
    return invocation, request, blob, owner


def response(repo, scope, root, bound, invocation, request):
    body = b'{"id":"resp_SYNTHETIC","status":"completed"}'
    blob = repo.save_blob(scope[0], io.BytesIO(body), "application/json")
    payload = dict(
        contract="model-invocation-event-v1",
        invocation_id=str(invocation),
        event_kind="provider_response",
        request_ref=request.model_dump(mode="json"),
        data={},
        response_blob=blob.model_dump(mode="json"),
    )
    return publish(repo, scope[0], root, bound, payload, [OwnedBlob(blob, body)]), blob


def test_request_alias_withdrawn_before_receipt_cannot_attach_unreadable_parent(repo, scope, producer):
    from podgen.persistence.dependencies import DependencyLedger

    _, root, bound, _, _ = inputs(repo, scope)
    invocation, request, _, owner = owned_request(repo, scope, root, bound, producer)
    DependencyLedger(repo).invalidate(scope[0], owner.ref, reason="SYNTHETIC withdrawal", category="access")
    with pytest.raises(AccessDenied):
        response(repo, scope, root, bound, invocation, request)


def test_request_alias_withdrawal_closes_receipt_and_parent_never_grants_bytes(repo, scope, producer):
    from podgen.persistence.dependencies import DependencyLedger

    journal, root, bound, _, _ = inputs(repo, scope)
    invocation, request, parent_blob, owner = owned_request(repo, scope, root, bound, producer)
    receipt, blob = response(repo, scope, root, bound, invocation, request)
    assert journal.read_blob(receipt, blob)
    with pytest.raises(AccessDenied):
        journal.read_blob(receipt, parent_blob)
    DependencyLedger(repo).invalidate(scope[0], owner.ref, reason="SYNTHETIC withdrawal", category="access")
    for ref in (request, receipt):
        with pytest.raises(AccessDenied):
            journal.read(ref)
    with repo.database.transaction() as tx:
        assert query(
            tx,
            "SELECT 1 FROM execution_record_withdrawals WHERE project_id=:p AND record_id=:r",
            p=scope[0],
            r=receipt.record_id,
        ).first()


def test_control_edges_are_not_read_dependencies_cold_or_warm(repo, scope, producer, monkeypatch):
    journal, root, bound, _, _ = inputs(repo, scope)
    invocation, request, _, _ = owned_request(repo, scope, root, bound, producer)
    receipt, _ = response(repo, scope, root, bound, invocation, request)
    from podgen.persistence import runtime_frontier

    original = runtime_frontier.load
    seen = []

    def load(tx, project, identities, known):
        assert request.record_id not in identities, "privacy-only parent loaded as semantic ancestry"
        seen.extend(identities)
        return original(tx, project, identities, known)

    monkeypatch.setattr(runtime_frontier, "load", load)
    with journal.runtime_graph():
        for _ in range(2):
            assert journal.read(receipt)["event_kind"] == "provider_response"
            repo.authorize_runtime_many(scope[0], [receipt])
    assert receipt.record_id in seen
