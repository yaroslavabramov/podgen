import copy
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import DependencyDisposition
from podgen.domain.contracts.runtime import DependencyRevalidationEvent
from podgen.domain.errors import Conflict
from podgen.episodes.branches import EditorialBranches
from podgen.persistence.database import query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.repository import timestamp
from tests.factories import Factory


def test_accepted_note_disposition_requires_exact_editor_integration(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    source, _ = f.source("ТЕСТОВАЯ ошибочная опора")
    old = f.make("ResearchNote", f.note().payload, input_refs=[source.ref])
    fixed = f.make(
        "ResearchNote",
        old.payload
        | {
            "answer_markdown": "ТЕСТОВАЯ исправленная заметка",
            "supersedes_note_refs": [old.ref.model_dump(mode="json")],
        },
        artifact_id=old.ref.artifact_id,
    )
    ledger = DependencyLedger(repo)
    ledger.invalidate(f.project, source.ref, category="quality", reason="ТЕСТОВАЯ ошибка")
    assert ledger.quality_blockers(f.project, fixed.ref)
    disposition = DependencyDisposition(
        prior_input_ref=source.ref,
        current_input_refs=[],
        disposition="removed",
        subject_selectors=[dict(artifact_ref=fixed.ref, whole=True)],
        basis="ТЕСТ: проверена вся новая заметка",
    )
    decision = f.make(
        "EditorialDecision",
        f.decision().payload
        | {
            "reviewed_output_refs": [fixed.ref.model_dump(mode="json")],
            "dependency_dispositions": [
                dict(
                    subject_ref=fixed.ref.model_dump(mode="json"),
                    dispositions=[disposition.model_dump(mode="json")],
                )
            ],
        },
    )
    state = copy.deepcopy(f.state.payload)
    state["current_decision_refs"] = [decision.ref.model_dump(mode="json")]
    branches = EditorialBranches(repo)
    next_, integration = branches.integrate(
        f.project,
        f.episode,
        f.branch_id,
        f.state.ref,
        0,
        decision.ref,
        state,
        producer=producer,
        operation_id=uuid4(),
    )
    event = DependencyRevalidationEvent(
        event_id=uuid4(),
        subject_ref=fixed.ref,
        assessment_ref=decision.ref,
        authority=dict(kind="integration_event", record_id=integration),
        dispositions=[disposition],
        expected_validity_version=ledger.validity_version(f.project),
        policy_ref=f.bundle.ref,
        created_at=timestamp(),
    )
    ledger.revalidate(f.project, event)
    assert ledger.quality_blockers(f.project, fixed.ref) == []
    with pytest.raises(Conflict, match="branch_head_or_epoch_conflict"):
        branches.integrate(
            f.project,
            f.episode,
            f.branch_id,
            f.state.ref,
            0,
            decision.ref,
            state,
            producer=producer,
            operation_id=uuid4(),
        )
    assert repo.get(f.project, f.state.ref).payload["current_decision_refs"] == []

    assert ledger.revalidate(f.project, event) == event
    with repo.database.transaction() as tx:
        query(
            tx,
            "UPDATE branch_heads SET generation_epoch=1 WHERE project_id=:p AND branch_id=:b",
            p=f.project,
            b=f.branch_id,
        )
    with pytest.raises(Conflict, match="integration_generation_epoch_changed"):
        ledger.revalidate(f.project, event)
