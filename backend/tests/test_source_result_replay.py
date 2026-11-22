"""An existing local result is a freshly authorized metadata read, not a source replay."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.runtime.evaluation import source_result_replay as replay


@pytest.fixture
def published(monkeypatch):
    run, invocation, step, event_id, candidate, result = (uuid4() for _ in range(6))

    def ref(identity):
        return RuntimeRef(record_kind="tool_execution", record_id=identity).model_dump(mode="json")

    outcome = dict(
        run_id=str(run),
        invocation_id=str(invocation),
        call_id="exact_call",
        next_turn=87,
        next_request_ref=ref(candidate),
        tool_result_ref=ref(result),
        request_hash="saved-hash",
    )
    event = dict(
        invocation_id=str(invocation),
        data=dict(policy=replay.VERSION, call_id="exact_call", turn_index=86, outcome=outcome),
    )
    prepared = dict(
        contract=replay.VERSION,
        stage="fact-review",
        next_turn=87,
        request_hash="saved-hash",
        tool_result_ref=ref(result),
    )
    fact = dict(
        archive_views=[
            dict(
                record_kind="tool_execution",
                record_id=str(event_id),
                event=dict(kind="source_material_result_recovered"),
            ),
            dict(record_id=str(candidate)),
            dict(record_id=str(result)),
        ]
    )
    head = dict(active_attempt_id=invocation, active_step_id=step, step_index=86, phase="tool_running")
    reads = []

    def read(reference):
        reads.append(reference.record_id)
        return event if reference.record_id == event_id else prepared

    journal = SimpleNamespace(
        repo=None, project=uuid4(), execution=SimpleNamespace(current_head=lambda *a: head), read=read
    )
    monkeypatch.setattr(replay, "facts", lambda *a, **kw: [fact])
    return SimpleNamespace(**locals())


def test_repeat_reads_only_atomic_result_metadata(published):
    p = published
    assert replay.existing(p.journal, p.run, "fact-review", p.invocation, "exact_call") == p.outcome
    assert p.reads == [p.event_id, p.candidate]


@pytest.mark.parametrize("changed", ["stage", "call", "ownership", "hash", "phase"])
def test_existing_result_remains_bound_to_its_operation(published, changed):
    p = published
    stage, call = "fact-review", "exact_call"
    if changed == "stage":
        stage = "reader-review"
    elif changed == "call":
        call = "foreign_call"
    elif changed == "ownership":
        p.fact["archive_views"][1]["record_id"] = str(uuid4())
    elif changed == "hash":
        p.prepared["request_hash"] = "changed"
    else:
        p.head["phase"] = "outcome_unknown"
    with pytest.raises(ContractError, match="source_result_recovery_evidence_mismatch"):
        replay.existing(p.journal, p.run, stage, p.invocation, call)


def test_fresh_permission_failure_on_repeat_is_fatal(published):
    p = published
    original = deepcopy(p.outcome)

    def denied(reference):
        raise AccessDenied()

    p.journal.read = denied
    with pytest.raises(AccessDenied):
        replay.existing(p.journal, p.run, "fact-review", p.invocation, "exact_call")
    assert p.outcome == original
