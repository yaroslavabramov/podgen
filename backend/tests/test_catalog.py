import io
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import ArtifactRef, HumanProducer
from podgen.domain.contracts.runtime import AcceptanceRecord, PublishedAsset
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.catalog import ConfigRefResolver
from podgen.persistence.database import query
from podgen.persistence.decisions import AcceptanceLedger
from podgen.persistence.repository import Repository, timestamp
from tests.factories import Factory


@pytest.fixture
def catalog(repo, scope, producer):
    tenant, episode = scope
    project = uuid4()
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'catalog')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'operator')", p=project, a=repo.actor_id)
        query(tx, "INSERT INTO episodes VALUES(:p,:e)", p=project, e=episode)
    factory = Factory(repo, project, episode, producer).setup()
    return factory, tenant


def test_published_exact_configuration_only_and_catalog_revocation(catalog):
    f, tenant = catalog
    resolver = ConfigRefResolver(f.repo)
    source_ref = ArtifactRef.model_validate(f.policies["source"])
    with pytest.raises(AccessDenied):
        resolver.resolve(tenant, source_ref, expected_kind="Policy")
    publication = resolver.publish(f.project, source_ref, [tenant])
    assert (
        resolver.resolve(tenant, source_ref, expected_kind="Policy", expected_subtype="source").ref
        == source_ref
    )
    with pytest.raises(ContractError, match="configuration_subtype_mismatch"):
        resolver.resolve(tenant, source_ref, expected_kind="Policy", expected_subtype="audio")
    with pytest.raises(AccessDenied):
        resolver.resolve(
            tenant, source_ref.model_copy(update={"kind": "ModelProfile"}), expected_kind="ModelProfile"
        )
    with pytest.raises(AccessDenied):
        resolver.publish(f.project, f.note().ref, [tenant])
    # Tenant artifact gets a typed catalog link, not a bypass of tenant FK.
    tenant_note = f.repo.create(
        tenant,
        "ResearchNote",
        f.note().payload,
        producer=f.producer,
        operation_id=uuid4(),
        input_refs=[source_ref],
    )
    assert f.repo.get(tenant, tenant_note.ref)
    resolver.revoke(f.project, publication.publication_id, "test-withdraw-publication")
    with pytest.raises(AccessDenied):
        f.repo.get(tenant, tenant_note.ref)


def test_private_custom_voice_is_not_catalog_publishable(catalog):
    f, tenant = catalog
    private = f.make(
        "VoiceProfile",
        f.voice.payload
        | {"voice_kind": "authorized_custom", "authorization_ref": f.bundle.ref.model_dump(mode="json")},
    )
    with pytest.raises(AccessDenied):
        ConfigRefResolver(f.repo).publish(f.project, private.ref, [tenant])
    # Generation principal cannot elevate membership into catalog publishing.
    with f.repo.database.transaction() as tx:
        query(
            tx,
            "UPDATE project_members SET role='editor' WHERE project_id=:p AND actor_id=:a",
            p=f.project,
            a=f.repo.actor_id,
        )
    with pytest.raises(AccessDenied):
        ConfigRefResolver(f.repo).publish(
            f.project, ArtifactRef.model_validate(f.policies["source"]), [tenant]
        )


def test_safe_projection_and_explicit_audition_asset(catalog):
    f, tenant = catalog
    resolver = ConfigRefResolver(f.repo)
    data = ArtifactRef.model_validate(f.policies["data"])
    resolver.publish(f.project, data, [tenant])
    blob = f.repo.save_blob(f.project, io.BytesIO(b"TEST ONLY: no real audition"), "audio/test-only")
    publication = resolver.publish(
        f.project,
        f.model.ref,
        [tenant],
        published_assets=[
            PublishedAsset(blob_ref=blob, media_metadata={"test_data": True}, allowed_roles=["evaluator"])
        ],
    )
    report = f.make(
        "CapabilityReport",
        dict(
            profile_ref=f.model.ref.model_dump(mode="json"),
            tested_configuration_fingerprint=f.model.content_hash,
            adapter_version="test-v1",
            checks=[
                dict(capability="test-only", expected=True, observed=True, outcome="pass", diagnostic_refs=[])
            ],
            observed_limits={},
            probe_run_ids=[],
            probe_invocation_ids=[],
            limitations=["SYNTHETIC; not a real model test"],
        ),
    )
    record = AcceptanceRecord(
        record_id=uuid4(),
        subject_ref=f.model.ref,
        check_kind="profile_qualification",
        decision="accepted",
        policy_ref=f.bundle.ref,
        reviewed_input_refs=[report.ref, f.evaluation_result(f.model).ref],
        assessment_scope=dict(
            supported_cells=[
                dict(
                    roles=["writer"],
                    language="ru",
                    format="dialogue",
                    duration_interval=dict(min_seconds=60, max_seconds=60),
                    companion_profile_refs=[],
                )
            ]
        ),
        findings=[],
        reviewer_ref=HumanProducer(kind="human_review", review_session_id=uuid4(), rater_id=f.repo.actor_id),
        created_at=timestamp(),
    )
    AcceptanceLedger(f.repo).append(f.project, record)
    assert (
        resolver.resolve_for_cell(
            tenant,
            f.model.ref,
            expected_kind="ModelProfile",
            role="writer",
            language="ru",
            format="dialogue",
            duration_seconds=60,
        ).ref
        == f.model.ref
    )
    with pytest.raises(ContractError, match="profile_not_qualified_for_cell"):
        resolver.resolve_for_cell(
            tenant,
            f.model.ref,
            expected_kind="ModelProfile",
            role="writer",
            language="ru",
            format="dialogue",
            duration_seconds=60,
            companion_profile_refs=[f.voice.ref],
        )
    for field, value in [
        ("language", "en"),
        ("format", "solo"),
        ("duration_seconds", 61),
        ("role", "researcher"),
    ]:
        kwargs = dict(
            expected_kind="ModelProfile", role="writer", language="ru", format="dialogue", duration_seconds=60
        )
        kwargs[field] = value
        with pytest.raises(ContractError, match="profile_not_qualified_for_cell"):
            resolver.resolve_for_cell(tenant, f.model.ref, **kwargs)
    for inputs in [[report.ref], [record.reviewed_input_refs[1]]]:
        with pytest.raises(ContractError, match="qualification_requires_exact_capability_and_evaluation"):
            AcceptanceLedger(f.repo).append(
                f.project, record.model_copy(update={"record_id": uuid4(), "reviewed_input_refs": inputs})
            )
    wrong = record.assessment_scope.model_copy(
        update={
            "supported_cells": [
                record.assessment_scope.supported_cells[0].model_copy(update={"language": "en"})
            ]
        }
    )
    with pytest.raises(ContractError, match="qualification_cell_has_no_matching_evaluation"):
        AcceptanceLedger(f.repo).append(
            f.project, record.model_copy(update={"record_id": uuid4(), "assessment_scope": wrong})
        )
    projection = resolver.qualification(tenant, f.model.ref)
    assert set(projection) == {"profile_ref", "qualified_scopes"}
    assert len(projection["qualified_scopes"]) == 1
    assert "diagnostic_refs" not in str(projection)
    assert resolver.read_published_asset(tenant, f.model.ref, blob.sha256, role="evaluator").startswith(
        b"TEST ONLY"
    )
    with pytest.raises(AccessDenied):
        resolver.read_published_asset(tenant, f.model.ref, blob.sha256, role="writer")
    with pytest.raises(AccessDenied):
        f.repo.read_blob(tenant, f.model.ref, blob)
    resolver.revoke(f.project, publication.publication_id, "test")
    with pytest.raises(AccessDenied):
        resolver.read_published_asset(tenant, f.model.ref, blob.sha256, role="evaluator")


def test_evaluation_gold_is_denied_through_artifact_and_runtime_wrappers(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    gold = f.make(
        "EvaluationAnnotation",
        dict(
            episode_spec_ref=f.spec.ref.model_dump(mode="json"),
            source_snapshot_refs=[],
            source_assertions=[],
            expected_understanding=[],
            forbidden_inferences=[],
            known_uncertainties=[],
            scoring_policy_ref=f.policies["evaluation"],
            annotator_assignment_ids=[],
        ),
    )
    with pytest.raises(AccessDenied):
        f.make("ResearchNote", f.note().payload, input_refs=[gold.ref])
    wrapper = f.make("ResearchNote", f.note().payload, input_refs=[gold.ref], audience="evaluator")
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    for ref in (gold.ref, wrapper.ref):
        with pytest.raises(AccessDenied):
            generation.get(f.project, ref)
    from podgen.domain.contracts.common import RuntimeRef

    runtime = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    with pytest.raises(AccessDenied):
        repo.append_runtime(f.project, runtime, {"gold": gold.ref.model_dump(mode="json")})
    repo.append_runtime(f.project, runtime, {"gold": gold.ref.model_dump(mode="json")}, audience="evaluator")
    with pytest.raises(AccessDenied):
        generation.get_runtime(f.project, runtime)


def test_revoked_published_audition_cannot_be_read_through_another_publication(catalog):
    f, tenant = catalog
    resolver = ConfigRefResolver(f.repo)
    resolver.publish(f.project, ArtifactRef.model_validate(f.policies["data"]), [tenant])
    blob = f.repo.save_blob(f.project, io.BytesIO(b"SYNTHETIC private audition"), "audio/test")
    asset = PublishedAsset(blob_ref=blob, media_metadata={"test_data": True}, allowed_roles=["evaluator"])
    resolver.publish(f.project, f.model.ref, [tenant], published_assets=[asset])
    alias = f.make("ModelProfile", f.model.payload | {"model_id": "synthetic-audition-alias"})
    resolver.publish(f.project, alias.ref, [tenant], published_assets=[asset])
    assert resolver.read_published_asset(tenant, alias.ref, blob.sha256, role="evaluator")
    from podgen.persistence.dependencies import DependencyLedger

    DependencyLedger(f.repo).invalidate(
        f.project, f.model.ref, category="access", reason="SYNTHETIC audition withdrawal"
    )
    with pytest.raises(AccessDenied):
        resolver.read_published_asset(tenant, alias.ref, blob.sha256, role="evaluator")


def test_exact_nonempty_companion_combination_and_private_scope_projection(catalog):
    f, tenant = catalog
    resolver = ConfigRefResolver(f.repo)
    resolver.publish(f.project, ArtifactRef.model_validate(f.policies["data"]), [tenant])
    resolver.publish(f.project, f.model.ref, [tenant])
    companion = f.make("ModelProfile", f.model.payload | {"model_id": "synthetic-companion"})
    extra = f.make("ModelProfile", f.model.payload | {"model_id": "synthetic-extra"})
    cap = f.make(
        "CapabilityReport",
        dict(
            profile_ref=f.model.ref.model_dump(mode="json"),
            tested_configuration_fingerprint=f.model.content_hash,
            adapter_version="test",
            checks=[
                dict(capability="test", expected=True, observed=True, outcome="pass", diagnostic_refs=[])
            ],
            observed_limits={},
            probe_run_ids=[],
            probe_invocation_ids=[],
            limitations=["SYNTHETIC"],
        ),
    )
    result = f.evaluation_result(f.model, [companion])
    record = AcceptanceRecord(
        record_id=uuid4(),
        subject_ref=f.model.ref,
        check_kind="profile_qualification",
        decision="accepted",
        policy_ref=f.bundle.ref,
        reviewed_input_refs=[cap.ref, result.ref],
        assessment_scope=dict(
            supported_cells=[
                dict(
                    roles=["writer"],
                    language="ru",
                    format="dialogue",
                    duration_interval=dict(min_seconds=60, max_seconds=60),
                    companion_profile_refs=[companion.ref],
                )
            ]
        ),
        findings=[],
        reviewer_ref=HumanProducer(kind="human_review", review_session_id=uuid4(), rater_id=f.repo.actor_id),
        created_at=timestamp(),
    )
    AcceptanceLedger(f.repo).append(f.project, record)
    assert resolver.qualification(tenant, f.model.ref)["qualified_scopes"] == []
    resolver.publish(f.project, companion.ref, [tenant])
    kwargs = dict(
        expected_kind="ModelProfile", role="writer", language="ru", format="dialogue", duration_seconds=60
    )
    assert (
        resolver.resolve_for_cell(tenant, f.model.ref, companion_profile_refs=[companion.ref], **kwargs).ref
        == f.model.ref
    )
    for companions in ([], [companion.ref, extra.ref]):
        with pytest.raises(ContractError, match="profile_not_qualified_for_cell"):
            resolver.resolve_for_cell(tenant, f.model.ref, companion_profile_refs=companions, **kwargs)
    from podgen.persistence.dependencies import DependencyLedger

    DependencyLedger(f.repo).invalidate(
        f.project, companion.ref, category="access", reason="SYNTHETIC companion privacy"
    )
    assert resolver.qualification(tenant, f.model.ref)["qualified_scopes"] == []
