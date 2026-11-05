from uuid import uuid4

import pytest

from podgen.domain.contracts.common import HumanProducer
from podgen.domain.contracts.runtime import AcceptanceRecord, DependencyRevalidationEvent, RevocationRecord
from podgen.domain.contracts.writing import ScriptRevision
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.spoken import spoken_view
from podgen.persistence.decisions import AcceptanceLedger
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.repository import timestamp
from tests.factories import Factory


@pytest.fixture
def setup(repo, scope, producer):
    return Factory(repo, *scope, producer).setup()


def report(f, script, source, disposition="removed"):
    view = spoken_view(script.ref, ScriptRevision.model_validate(script.payload))
    selectors = [
        dict(script_ref=script.ref.model_dump(mode="json"), selector=u["selector"])
        for u in view["utterances"]
    ]
    claims = f.make(
        "ClaimSet",
        dict(
            script_ref=script.ref.model_dump(mode="json"),
            claims=[],
            coverage=[],
            extraction_profile_ref=f.model.ref.model_dump(mode="json"),
            unresolved_extraction_questions=[],
        ),
    )
    # extraction_profile_ref is a ProcessingProfile, not the writer's model configuration.
    return f.make(
        "VerificationReport",
        dict(
            script_ref=script.ref.model_dump(mode="json"),
            claim_set_ref=claims.ref.model_dump(mode="json"),
            coverage_audit=dict(
                reviewer_run_ids=[str(uuid4())],
                reviewed_script_ranges=selectors,
                findings=[],
                unresolved_omissions=[],
            ),
            claim_reviews=[],
            cross_claim_findings=[],
            source_family_assessment_refs=[],
            calculation_refs=[],
            policy_ref=f.bundle.ref.model_dump(mode="json"),
            recommendation="accepted",
            remaining_work=[],
            dependency_dispositions=[
                dict(
                    prior_input_ref=source.ref.model_dump(mode="json"),
                    current_input_refs=[],
                    affected_selectors=selectors,
                    disposition=disposition,
                    basis="ТЕСТ: проверен весь исправленный текст; ошибочная опора больше не используется",
                    checked_against_script_ref=script.ref.model_dump(mode="json"),
                    reviewer_run_id=str(uuid4()),
                )
            ],
        ),
    )


def accepted(f, script, review):
    reviewer = HumanProducer(kind="human_review", review_session_id=uuid4(), rater_id=f.repo.actor_id)
    record = AcceptanceRecord(
        record_id=uuid4(),
        subject_ref=script.ref,
        check_kind="factual",
        decision="accepted",
        policy_ref=f.bundle.ref,
        reviewed_input_refs=[review.ref],
        assessment_scope=None,
        findings=[],
        reviewer_ref=reviewer,
        created_at=timestamp(),
    )
    AcceptanceLedger(f.repo).append(f.project, record)
    return record


def event_for(f, script, review, acceptance, source):
    body = review.payload["dependency_dispositions"][0]
    return DependencyRevalidationEvent(
        event_id=uuid4(),
        subject_ref=script.ref,
        assessment_ref=review.ref,
        authority=dict(kind="acceptance_record", record_id=acceptance.record_id),
        dispositions=[
            dict(
                prior_input_ref=source.ref,
                current_input_refs=[],
                disposition=body["disposition"],
                subject_selectors=body["affected_selectors"],
                basis=body["basis"],
            )
        ],
        expected_validity_version=DependencyLedger(f.repo).validity_version(f.project),
        policy_ref=f.bundle.ref,
        created_at=timestamp(),
    )


def test_ac03_corrected_script_can_leave_quality_invalidation_without_losing_parent(setup):
    f = setup
    source, _ = f.source("ТЕСТОВАЯ неверная опора: точные атомные часы обязательны в приёмнике.")
    first = f.script(input_refs=[source.ref], text="ТЕСТ: приёмник якобы обязан иметь атомные часы.")
    ledger = DependencyLedger(f.repo)
    ledger.invalidate(f.project, source.ref, reason="ТЕСТ: неверная инженерная опора", category="quality")
    next_ = f.script(parent=first)
    assert ledger.quality_blockers(f.project, next_.ref)
    profile = f.make(
        "ProcessingProfile",
        dict(
            profile_kind="extraction",
            implementation="test-only",
            version="1",
            configuration={},
            input_profile_refs=[],
        ),
    )
    # report helper consumes an explicit extraction profile.
    f.model = profile
    review = report(f, next_, source)
    record = accepted(f, next_, review)
    event = event_for(f, next_, review, record, source)
    ledger.revalidate(f.project, event)
    assert ledger.quality_blockers(f.project, next_.ref) == []
    assert ledger.quality_blockers(f.project, first.ref)
    assert f.repo.get(f.project, next_.ref).payload["parent_script_ref"] == first.ref.model_dump(mode="json")
    assert source.ref in f.repo.get(f.project, first.ref).input_refs
    # Revoking authority restores conservative dependencies; history remains intact.
    AcceptanceLedger(f.repo).revoke(
        f.project,
        RevocationRecord(
            revocation_id=uuid4(),
            acceptance_record_id=record.record_id,
            reason_code="test_recheck",
            basis_refs=[],
            reviewer_ref=record.reviewer_ref,
            created_at=timestamp(),
        ),
    )
    assert ledger.quality_blockers(f.project, next_.ref)
    ledger.invalidate(f.project, source.ref, reason="ТЕСТ: отозван private источник", category="access")
    with pytest.raises(AccessDenied):
        f.repo.get(f.project, next_.ref)


def test_unaccepted_forged_disposition_and_stale_version_rejected(setup):
    f = setup
    source, _ = f.source()
    first = f.script(input_refs=[source.ref])
    next_ = f.script(parent=first)
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
    review = report(f, next_, source)
    record = accepted(f, next_, review)
    ledger = DependencyLedger(f.repo)
    event = event_for(f, next_, review, record, source)
    wrong = event.model_copy(update={"authority": event.authority.model_copy(update={"record_id": uuid4()})})
    with pytest.raises(AccessDenied):
        ledger.revalidate(f.project, wrong)
    bad = event.model_copy(
        update={"dispositions": [event.dispositions[0].model_copy(update={"basis": "Автор сам решил"})]}
    )
    with pytest.raises(ContractError, match="dispositions_do_not_match"):
        ledger.revalidate(f.project, bad)
    f.note()
    with pytest.raises(Conflict, match="validity_version_conflict"):
        ledger.revalidate(f.project, event)


def test_retained_dependency_resolves_only_reviewed_quality_impacts(setup):
    f = setup
    source, _ = f.source(
        "ТЕСТ: документ содержит прежнее ошибочное утверждение, которое можно цитировать как историческое."
    )
    first = f.script(input_refs=[source.ref])
    fixed = f.script(
        parent=first,
        text="ТЕСТ: мы рассказываем о прежнем утверждении документа, а не принимаем его за истину.",
    )
    ledger = DependencyLedger(f.repo)
    ledger.invalidate(f.project, source.ref, reason="ТЕСТ: первый correction", category="quality")
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
    review = report(f, fixed, source, disposition="retained")
    record = accepted(f, fixed, review)
    event = event_for(f, fixed, review, record, source)
    ledger.revalidate(f.project, event)
    assert ledger.quality_blockers(f.project, fixed.ref) == []
    assert ledger.quality_blockers(f.project, first.ref)
    ledger.invalidate(f.project, source.ref, reason="ТЕСТ: новая независимая correction", category="quality")
    assert ledger.quality_blockers(f.project, fixed.ref)
