"""Late financial receipts and passive observations cannot reactivate an old owner."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid5

import pytest

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.persistence.database import query
from podgen.persistence.private_journal.accounting import effective
from podgen.persistence.private_journal.contracts import operation_id
from podgen.persistence.private_journal.state import head
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.technical_recovery import evidence, validate_retry
from podgen.runtime.evaluation.technical_recovery_policy import MAX_STAGE_REPLACEMENTS, VERSION
from podgen.runtime.models.contracts import ProviderFault, WireReply
from podgen.runtime.models.gateway import ModelGateway
from tests.test_model_step_publication import run, setup


def _reply(body, input_tokens=100, output_tokens=10):
    return WireReply(
        json.dumps(
            dict(
                id=f"resp_{body['metadata'].get('podgen_invocation_id', 'parent')}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[],
                usage=dict(input_tokens=input_tokens, output_tokens=output_tokens),
            )
        ).encode()
    )


def _coordinator(row):
    return {
        key: value for key, value in row.items() if key not in ("revision", "last_entry_id", "accounting")
    }


def _case(repo, scope, child_status):
    journal, root, _, _, _, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    body, original = (
        snapshot[0],
        journal.read(RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"])),
    )
    calls = dict(count=0, submit=0)

    def count(body):
        calls["count"] += 1
        return WireReply(b'{"input_tokens":100}')

    def lost(body, **options):
        calls["submit"] += 1
        raise ProviderFault("provider_transport_outcome_unknown", ambiguous=True)

    provider = SimpleNamespace(count=count, submit=lost)
    gateway = ModelGateway(journal, lambda _: provider)
    with pytest.raises(RunFailure, match="provider_transport_outcome_unknown"):
        run(gateway, root, profile, limits, request_snapshot=(body, original), manifest_snapshot=manifest)
    parent = journal.invocation(root["run_id"], "draft")
    parent_head = head(repo, scope[0], root["run_id"])
    assert parent["status"] == "outcome_unknown"
    child_id = uuid5(parent["invocation_id"], "technical-recovery:1")
    child_body = deepcopy(body)
    child_body["metadata"].update(
        podgen_invocation_id=str(child_id),
        podgen_attempt_index="1",
        podgen_parent_invocation_id=str(parent["invocation_id"]),
    )
    child = journal.prepare(
        parent["run_id"],
        "draft",
        child_body,
        deepcopy(original["context"]),
        attempt_index=1,
        parent_invocation_id=parent["invocation_id"],
        attempt_kind="technical_retry",
        operation_kind="role",
        authorization=dict(
            kind=VERSION,
            actor_id=str(repo.actor_id),
            target_invocation_id=str(parent["invocation_id"]),
            maximum_new_attempts=1,
            maximum_stage_replacements=MAX_STAGE_REPLACEMENTS,
            evidence=evidence(journal, parent),
        ),
    )
    if child_status != "prepared":

        def submit_child(body, **options):
            calls["submit"] += 1
            if child_status == "submitting":
                raise RuntimeError("child acknowledgement lost")
            return _reply(body)

        provider.submit = submit_child
        if child_status == "submitting":
            with pytest.raises(RuntimeError, match="child acknowledgement lost"):
                run(gateway, child, profile, limits, manifest_snapshot=manifest)
        else:
            run(gateway, child, profile, limits, manifest_snapshot=manifest)
        child = journal.invocation(root["run_id"], "draft")
    assert child["status"] == child_status
    return SimpleNamespace(
        journal=journal,
        gateway=gateway,
        parent=parent,
        child=child,
        body=body,
        calls=calls,
        submit_entry=parent_head["submit_entry_id"],
    )


def test_active_receipts_reuse_head_without_lookup_and_keep_observation_sequence(repo, scope, monkeypatch):
    from podgen.runtime.evaluation import checkpoint_receipts

    journal, root, _, _, _, _, manifest, profile, limits, _ = setup(repo, scope)
    calls = dict(count=0, submit=0)

    def count(body):
        calls["count"] += 1
        return WireReply(b'{"input_tokens":100}')

    def submit(body, **options):
        calls["submit"] += 1
        return _reply(body)

    def forbidden(*args, **kwargs):
        pytest.fail("active receipt reloaded its run-owned submit binding")

    monkeypatch.setattr(checkpoint_receipts, "one", forbidden)
    provider = SimpleNamespace(count=count, submit=submit)
    gateway = ModelGateway(journal, lambda _: provider)
    run(gateway, root, profile, limits, manifest_snapshot=manifest)
    parent = journal.invocation(root["run_id"], "draft")
    body = journal.request(parent)
    for index in (1, 2):
        journal.event(parent, "active_probe", data=dict(index=index))
        gateway._archive_reply(parent, _reply(body, 100 - index, 10), body)
        assert head(repo, scope[0], root["run_id"])["observation_no"] == index
    assert calls == dict(count=1, submit=1)
    with repo.database.read_transaction() as tx:
        observations = list(
            query(
                tx,
                """SELECT observation_no FROM execution_entries
                WHERE project_id=:p AND run_id=:r AND checkpoint_kind='provider_receipt'
                ORDER BY revision""",
                p=scope[0],
                r=root["run_id"],
            ).scalars()
        )
    assert observations == [0, 1, 2]


@pytest.mark.parametrize("child_status", ["prepared", "submitting", "response_completed"])
def test_late_parent_receipts_settle_exact_charge_and_preserve_child(repo, scope, child_status):
    case = _case(repo, scope, child_status)
    journal, parent = case.journal, case.parent
    before = head(repo, scope[0], parent["run_id"])
    calls = dict(case.calls)
    child_cost = 0 if child_status == "prepared" else 100
    child_output = 10 if child_status == "response_completed" else 0
    for index, (input_tokens, output_tokens) in enumerate(((90, 7), (95, 8))):
        case.gateway._archive_reply(parent, _reply(case.body, input_tokens, output_tokens), case.body)
        current = head(repo, scope[0], parent["run_id"])
        assert _coordinator(current) == _coordinator(before)
        assert current["accounting"]["input_tokens"] == input_tokens + child_cost
        assert current["accounting"]["output_tokens"] == output_tokens + child_output
        assert current["accounting"]["uncertain_calls"] == (child_status == "submitting")
        assert current["accounting"]["output_reserved"] == (1000 if child_status == "submitting" else 0)
        assert current["accounting"]["generation_calls"] == calls["submit"]
        # An intervening empty observation must not reset the parent's receipt
        # sequence to the child's observation number, nor steal its coordinator.
        journal.event(parent, "late_parent_probe", data=dict(index=index))
        assert _coordinator(head(repo, scope[0], parent["run_id"])) == _coordinator(before)
    assert case.calls == calls
    fresh = EvaluationJournal(repo, scope[0])
    rows = fresh.attempts(parent["run_id"], "draft")
    assert rows[-1] == case.child
    assert rows[0]["status"] == "outcome_unknown" and rows[0]["provider_response_id"] is None
    assert rows[0]["input_tokens"] == 95 and rows[0]["output_tokens"] == 8
    validate_retry(fresh, rows[-1], rows[0])
    operation = operation_id(parent["invocation_id"], "submit")
    with repo.database.read_transaction() as tx:
        receipts = list(
            query(
                tx,
                """SELECT observation_no,data FROM execution_entries
                WHERE project_id=:p AND run_id=:r AND operation_id=:op
                  AND checkpoint_kind='provider_receipt' ORDER BY observation_no""",
                p=scope[0],
                r=parent["run_id"],
                op=operation,
            ).mappings()
        )
        revision, charge = effective(tx, scope[0], parent["run_id"], operation)
    assert [row["observation_no"] for row in receipts] == [0, 1]
    assert all(row["data"]["submit_entry_id"] == str(case.submit_entry) for row in receipts)
    assert all(row["data"]["receipt_state"]["status"] == "response_completed" for row in receipts)
    assert revision == 3
    assert charge == dict(generation_calls=1, input_tokens=95, output_tokens=8)


def test_late_parent_overflow_preserves_child_and_raw_receipt(repo, scope):
    case = _case(repo, scope, "response_completed")
    before = head(repo, scope[0], case.parent["run_id"])
    reply = _reply(case.body, 90, 2**63 - 1)
    with pytest.raises(RunFailure, match="run_token_usage_overflow"):
        case.gateway._archive_reply(case.parent, reply, case.body)
    after = head(repo, scope[0], case.parent["run_id"])
    assert _coordinator(after) == _coordinator(before)
    assert after["accounting"] == before["accounting"]
    fresh = EvaluationJournal(repo, scope[0])
    assert fresh.invocation(case.parent["run_id"], "draft") == case.child
    events = fresh.events(case.parent)
    ref = RuntimeRef(record_kind="tool_execution", record_id=events[-1]["record_id"])
    record = fresh.read(ref)
    assert record["event_kind"] == "provider_response"
    assert fresh.read_blob(ref, BlobRef.model_validate(record["response_blob"])) == reply.body


@pytest.mark.parametrize("status", ["failed", "validation_failed", "outcome_unknown"])
def test_empty_physical_observation_preserves_warm_and_cold_status(repo, scope, status):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, history = setup(repo, scope)

    def lost(body, **options):
        raise RuntimeError("lost acknowledgement")

    provider = SimpleNamespace(count=lambda body: WireReply(b'{"input_tokens":100}'), submit=lost)
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        run(
            ModelGateway(tj, lambda _: provider),
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )
    turn = dict(turn, **model_turns.rows(journal, root)[0])
    turn = tj.transition(turn, status)
    before = head(repo, scope[0], root["run_id"])
    phase = tj.publication(turn)
    ref = phase.event("probe_empty", history=history)
    row = phase.commit()
    assert row["status"] == status
    assert _coordinator(head(repo, scope[0], root["run_id"])) == _coordinator(before)
    cached, _ = journal.execution.current_physical(root["run_id"], root["invocation_id"], 0)
    assert cached["status"] == status
    fresh = EvaluationJournal(repo, scope[0])
    restored = fresh.invocation(root["run_id"], "draft")
    assert restored["status"] == status
    assert model_turns.rows(fresh, restored)[0]["status"] == status
    with repo.database.read_transaction() as tx:
        saved = (
            query(
                tx,
                "SELECT checkpoint_kind,data FROM execution_entries WHERE project_id=:p AND archive_ids @> ARRAY[CAST(:id AS uuid)]",
                p=scope[0],
                id=ref.record_id,
            )
            .mappings()
            .one()
        )
    assert saved["checkpoint_kind"] == "observation" and "state" not in saved["data"]
