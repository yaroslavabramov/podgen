"""Native structural proof, legacy adoption and fresh authorization; no paid transport."""

from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import BlobRef, CodeProducer
from podgen.domain.errors import AccessDenied, Conflict
from podgen.persistence.database import one, query
from podgen.persistence.dependencies import DependencyLedger
from podgen.runtime.evaluation import reading_audit_replay, reading_validation
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.reading_outcome import audit_outcome, read_outcome
from podgen.runtime.evaluation.service import WritingEvaluation
from tests.episode_production_support import setup_episode
from tests.research_material_cases import saved
from tests.research_material_provider import ResearchProvider


def native(repo, scope, producer, monkeypatch, *, legacy=False):
    config, *_ = setup_episode(repo, scope, producer, monkeypatch)
    shared, *_ = saved(repo, scope, producer, monkeypatch)
    settings = config.settings
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        case_id="hosted-case",
        author_ref=shared,
        writer_profile_ref=settings.writer_profile_ref,
        reviewer_profile_ref=settings.reviewer_profile_ref,
        planner_profile_ref=settings.planner_profile_ref,
        plan_reviewer_profile_ref=settings.plan_reviewer_profile_ref,
        plan_mode="own-plan",
    )
    provider = ResearchProvider()
    if legacy:
        with monkeypatch.context() as prior:
            # Simulate the pre-proof publisher; all outputs and validation are native.
            prior.setattr(reading_validation, "certify", audit_outcome)
            result = WritingEvaluation(repo, lambda profile: provider).run(command)
    else:
        result = WritingEvaluation(repo, lambda profile: provider).run(command)
    assert result["review_status"] == "keep"
    return EvaluationJournal(repo, scope[0]), command.run_id


def count_proofs(repo, project, run_id):
    with repo.database.read_transaction() as tx:
        return one(
            tx,
            "SELECT count(*) n FROM writing_outcome_validations WHERE project_id=:p AND run_id=:r",
            p=project,
            r=run_id,
        )["n"]


def withdraw_blob(repo, project, blob):
    owner = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(owned_blob=blob.model_dump(mode="json"))),
        producer=CodeProducer(
            kind="code", component="outcome-proof-test", build_id="test-only", operation_id=uuid4()
        ),
        operation_id=uuid4(),
        audience="evaluator",
    )
    return DependencyLedger(repo).invalidate(
        project, owner.ref, reason="Exact source withdrawal", category="access"
    )


def test_new_outcome_validates_once_and_normal_cold_reads_do_not_replay_rounds(
    repo, scope, producer, monkeypatch
):
    calls = []
    replay = reading_audit_replay.audit_outcome
    monkeypatch.setattr(
        reading_audit_replay, "audit_outcome", lambda *args: (calls.append(1), replay(*args))[1]
    )
    journal, run_id = native(repo, scope, producer, monkeypatch)
    assert len(calls) == 1 and count_proofs(repo, scope[0], run_id) == 1
    statements = []

    def record(conn, cursor, sql, parameters, context, many):
        statements.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", record)
    try:
        cold = EvaluationJournal(repo, scope[0])
        monkeypatch.setattr(
            cold, "invocation", lambda *args: pytest.fail("ordinary read reconstructed a historical role")
        )
        expected = read_outcome(cold, run_id)
        assert read_outcome(cold, run_id) == expected
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", record)
    assert len(calls) == 1
    assert not any("data->'state' AS state" in sql for sql in statements)
    assert audit_outcome(journal, run_id) == expected and len(calls) == 2


def test_legacy_outcome_requires_full_validation_before_its_single_adoption(
    repo, scope, producer, monkeypatch
):
    journal, run_id = native(repo, scope, producer, monkeypatch, legacy=True)
    assert count_proofs(repo, scope[0], run_id) == 0
    calls = []
    replay = reading_audit_replay.audit_outcome
    monkeypatch.setattr(
        reading_audit_replay, "audit_outcome", lambda *args: (calls.append(1), replay(*args))[1]
    )
    expected = read_outcome(journal, run_id)
    assert expected["status"] == "keep" and len(calls) == 1
    assert read_outcome(EvaluationJournal(repo, scope[0]), run_id) == expected and len(calls) == 1
    assert count_proofs(repo, scope[0], run_id) == 1


def test_source_withdrawal_between_audit_and_commit_prevents_proof_publication(
    repo, scope, producer, monkeypatch
):
    journal, run_id = native(repo, scope, producer, monkeypatch, legacy=True)
    blob = BlobRef.model_validate(journal.manifest(run_id)["materials_blob"])
    publish = reading_validation._publish

    def withdraw_then_publish(*args):
        withdraw_blob(repo, scope[0], blob)
        return publish(*args)

    monkeypatch.setattr(reading_validation, "_publish", withdraw_then_publish)
    with pytest.raises(AccessDenied):
        read_outcome(journal, run_id)
    assert count_proofs(repo, scope[0], run_id) == 0


def test_proof_never_caches_acl_or_grants_access_after_source_revocation(repo, scope, producer, monkeypatch):
    journal, run_id = native(repo, scope, producer, monkeypatch)
    descriptor = reading_validation.read_descriptor(journal, run_id)
    blob = BlobRef.model_validate(journal.manifest(run_id)["materials_blob"])
    withdraw_blob(repo, scope[0], blob)
    with pytest.raises(AccessDenied):
        descriptor.authorize(journal)
    with pytest.raises(AccessDenied):
        read_outcome(journal, run_id)
    assert count_proofs(repo, scope[0], run_id) == 1


def test_competing_conflicting_proof_is_rejected_without_overwriting_it(repo, scope, producer, monkeypatch):
    journal, run_id = native(repo, scope, producer, monkeypatch, legacy=True)
    publish = reading_validation._publish

    def conflict_then_publish(*args):
        with repo.database.transaction() as tx:
            query(
                tx,
                "INSERT INTO writing_outcome_validations VALUES(:p,:r,:v,'{}'::jsonb,now())",
                p=scope[0],
                r=run_id,
                v=reading_validation.VERSION,
            )
        return publish(*args)

    monkeypatch.setattr(reading_validation, "_publish", conflict_then_publish)
    with pytest.raises(Conflict, match="binding_mismatch"):
        read_outcome(journal, run_id)
    with pytest.raises(Conflict, match="binding_mismatch"):
        read_outcome(journal, run_id)
