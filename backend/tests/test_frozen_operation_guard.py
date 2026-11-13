"""Frozen access boundaries do not reconstruct or reparse material packets."""

import io
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.errors import AccessDenied
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.operation_guard import OperationGuard
from tests.authorization_helpers import withdraw_blob
from tests.test_research_material_resolver import fixture


@pytest.mark.parametrize("withdraw", ["source", "fixed"])
def test_boundary_reauthorizes_frozen_references_without_rebuilding_materials(
    repo, scope, producer, monkeypatch, withdraw
):
    materials, _, _, _ = fixture(repo, scope, producer, monkeypatch)
    project, _ = scope
    journal = EvaluationJournal(repo, project)
    blob = repo.save_blob(project, io.BytesIO(b"fixed immutable plan"), "text/plain")
    owner = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules={"blob": blob.model_dump(mode="json")}),
        producer=producer,
        operation_id=uuid4(),
    )
    fixed = journal.record(dict(blob=blob.model_dump(mode="json")))
    materials = replace(materials, fixed_ref=fixed)
    checks = []
    guard = OperationGuard(
        generation=repo,
        journal=journal,
        command=SimpleNamespace(project_id=project),
        manifest={},
        materials=materials,
        loader=lambda _: pytest.fail("rebuilding frozen materials"),
        checkpoint=lambda: checks.append(True),
    )
    guard()
    assert len(checks) == 2
    if withdraw == "fixed":
        from podgen.persistence.dependencies import DependencyLedger

        DependencyLedger(repo).invalidate(
            project, owner.ref, reason="Test fixed plan withdrawal", category="access"
        )
    else:
        spec = next(spec for spec in materials.packet.documents if spec.purpose == "primary")
        from podgen.domain.contracts.common import BlobRef

        withdraw_blob(repo, project, BlobRef.model_validate(materials.document_bindings[spec.key]["blob"]))
    with pytest.raises(AccessDenied):
        guard()
