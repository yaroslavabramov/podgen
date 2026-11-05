from uuid import uuid4

import pytest

from podgen.domain.errors import AccessDenied, Conflict
from podgen.episodes.branches import EditorialBranches
from podgen.persistence.database import one, query
from podgen.persistence.dependencies import DependencyLedger
from tests.factories import Factory
from tests.test_dependencies import accepted, event_for, report


def test_integrate_lost_ack_replays_once_but_not_different_payload_or_epoch(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    decision = f.decision()
    state = f.state.payload | {"current_decision_refs": [decision.ref.model_dump(mode="json")]}
    branches = EditorialBranches(repo)
    operation = uuid4()

    def integrate(payload=state):
        return branches.integrate(
            f.project,
            f.episode,
            f.branch_id,
            f.state.ref,
            0,
            decision.ref,
            payload,
            producer=producer,
            operation_id=operation,
        )

    first, event = integrate()
    assert integrate() == (first, event)
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM integration_events WHERE project_id=:p", p=f.project)["n"] == 1
        )
    with pytest.raises(Conflict, match="operation_id_reused_with_different_input"):
        integrate(state | {"working_answer": "Different payload"})
    with repo.database.transaction() as tx:
        query(
            tx,
            "UPDATE branch_heads SET generation_epoch=1 WHERE project_id=:p AND branch_id=:b",
            p=f.project,
            b=f.branch_id,
        )
    with pytest.raises(Conflict, match="epoch_conflict"):
        integrate()


def test_integrate_replay_rechecks_access(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    source, _ = f.source()
    decision = f.make("EditorialDecision", f.decision().payload, input_refs=[source.ref])
    state = f.state.payload | {"current_decision_refs": [decision.ref.model_dump(mode="json")]}
    branches = EditorialBranches(repo)
    operation = uuid4()

    def run():
        return branches.integrate(
            f.project,
            f.episode,
            f.branch_id,
            f.state.ref,
            0,
            decision.ref,
            state,
            producer=producer,
            operation_id=operation,
        )

    run()
    DependencyLedger(repo).invalidate(f.project, source.ref, category="access", reason="SYNTHETIC privacy")
    with pytest.raises(AccessDenied):
        run()


def test_revalidation_lost_ack_idempotency_and_live_access(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    source, _ = f.source()
    first = f.script(input_refs=[source.ref])
    fixed = f.script(parent=first)
    f.model = f.make(
        "ProcessingProfile",
        dict(
            profile_kind="extraction",
            implementation="test",
            version="1",
            configuration={},
            input_profile_refs=[],
        ),
    )
    review = report(f, fixed, source)
    acceptance = accepted(f, fixed, review)
    event = event_for(f, fixed, review, acceptance, source)
    ledger = DependencyLedger(repo)
    assert ledger.revalidate(f.project, event) == event
    f.note("SYNTHETIC unrelated new revision changes validity version")
    assert ledger.revalidate(f.project, event) == event
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM dependency_revalidations WHERE project_id=:p", p=f.project)[
                "n"
            ]
            == 1
        )
    with pytest.raises(Conflict, match="event_id_reused_with_different_input"):
        ledger.revalidate(f.project, event.model_copy(update={"created_at": "2026-11-01T00:00:00Z"}))
    ledger.invalidate(f.project, source.ref, category="access", reason="SYNTHETIC privacy")
    with pytest.raises(AccessDenied):
        ledger.revalidate(f.project, event)
