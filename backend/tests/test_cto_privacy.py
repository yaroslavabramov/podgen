import io
from uuid import uuid4

import pytest

from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.dependencies import DependencyLedger
from tests.factories import Factory


def test_access_revocation_blocks_old_new_and_derived_blob_aliases(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    source, blob = f.source("SYNTHETIC private source full bytes")
    alias = f.make("SourceRevision", source.payload | {"source_id": str(uuid4())})
    derived_blob = repo.save_blob(f.project, io.BytesIO(b"SYNTHETIC derived private content"), "text/plain")
    derivative = f.make(
        "Policy",
        dict(
            policy_kind="data",
            rules={"input": source.ref.model_dump(mode="json"), "blob": derived_blob.model_dump(mode="json")},
        ),
    )
    derived_alias = f.make(
        "SourceRevision",
        source.payload | {"source_id": str(uuid4()), "original_blob": derived_blob.model_dump(mode="json")},
    )
    alias_derived_blob = repo.save_blob(
        f.project, io.BytesIO(b"SYNTHETIC derivative reached through old alias"), "text/plain"
    )
    f.make(
        "Policy",
        dict(
            policy_kind="data",
            rules={
                "input": alias.ref.model_dump(mode="json"),
                "blob": alias_derived_blob.model_dump(mode="json"),
            },
        ),
    )
    alias_derived_owner = f.make(
        "SourceRevision",
        source.payload
        | {"source_id": str(uuid4()), "original_blob": alias_derived_blob.model_dump(mode="json")},
    )
    assert repo.read_blob(f.project, alias.ref, blob)
    DependencyLedger(repo).invalidate(
        f.project, source.ref, reason="SYNTHETIC privacy withdrawal", category="access"
    )
    for owner, handle in [
        (source, blob),
        (alias, blob),
        (derivative, derived_blob),
        (derived_alias, derived_blob),
        (alias_derived_owner, alias_derived_blob),
    ]:
        with pytest.raises(AccessDenied):
            repo.get(f.project, owner.ref)
        with pytest.raises(AccessDenied):
            repo.read_blob(f.project, owner.ref, handle)
    for handle in (blob, derived_blob):
        with pytest.raises(AccessDenied):
            f.make(
                "SourceRevision",
                source.payload | {"source_id": str(uuid4()), "original_blob": handle.model_dump(mode="json")},
            )
    with pytest.raises(AccessDenied):
        repo.save_blob(
            f.project, io.BytesIO(b"SYNTHETIC private source full bytes"), "application/octet-stream"
        )


def test_quality_invalidation_does_not_revoke_blob_bytes(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    source, blob = f.source()
    DependencyLedger(repo).invalidate(
        f.project, source.ref, reason="SYNTHETIC quality correction", category="quality"
    )
    assert repo.read_blob(f.project, source.ref, blob)
    alias = f.make("SourceRevision", source.payload | {"source_id": str(uuid4())})
    assert repo.read_blob(f.project, alias.ref, blob)


def test_source_identity_create_revision_retry_and_mismatch(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    source, _ = f.source()
    assert str(source.ref.artifact_id) == source.payload["source_id"]
    operation = uuid4()
    payload = source.payload | {"title": "SYNTHETIC revised metadata"}
    first = repo.create(
        f.project, "SourceRevision", payload, producer=producer, operation_id=operation, episode_id=f.episode
    )
    second = repo.create(
        f.project, "SourceRevision", payload, producer=producer, operation_id=operation, episode_id=f.episode
    )
    assert first == second
    assert first.ref.artifact_id == source.ref.artifact_id
    assert first.ref.revision_id != source.ref.revision_id
    with pytest.raises(ContractError, match="source_identity_mismatch"):
        f.make("SourceRevision", payload, artifact_id=uuid4())


def test_source_identity_import_revision_and_retry_are_consistent(repo, scope, producer, tmp_path):
    from podgen.domain.packages import ArtifactPackage
    from podgen.persistence.database import one
    from podgen.persistence.packages import PackageImporter

    f = Factory(repo, *scope, producer).setup()
    source, _ = f.source()
    sid = str(uuid4())
    body = source.payload | {"source_id": sid, "acquisition_record_ref": {"$runtime": "acquisition"}}

    def item(key, payload, identity=None):
        return dict(
            key=key,
            kind="SourceRevision",
            payload=payload,
            input_keys=[identity] if identity else [],
            audience="project",
            identity_of=identity,
        )

    raw = dict(
        format="podgen-fixture-v1",
        package_id=str(uuid4()),
        test_data=True,
        description="SYNTHETIC source identity import",
        artifacts=[
            item("original", body),
            item("revised", body | {"title": "SYNTHETIC revised source"}, "original"),
        ],
        runtime_records=[],
        blobs=[],
        external_refs={"data": f.policies["data"]},
        external_runtime_refs={"acquisition": source.payload["acquisition_record_ref"]},
    )
    package = ArtifactPackage.model_validate(raw)
    importer = PackageImporter(repo)
    result = importer.import_package(*scope, package, tmp_path, producer=producer)
    assert len(result) == 2
    assert all(str(revision.ref.artifact_id) == sid == revision.payload["source_id"] for revision in result)
    assert result[0].ref.revision_id != result[1].ref.revision_id
    assert importer.import_package(*scope, package, tmp_path, producer=producer) == result
    before = None
    with repo.database.transaction() as tx:
        before = one(tx, "SELECT count(*) AS n FROM artifact_revisions WHERE project_id=:p", p=f.project)["n"]
    raw["package_id"] = str(uuid4())
    raw["artifacts"][1]["payload"]["source_id"] = str(uuid4())
    with pytest.raises(ContractError, match="source_identity_mismatch"):
        importer.import_package(*scope, ArtifactPackage.model_validate(raw), tmp_path, producer=producer)
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM artifact_revisions WHERE project_id=:p", p=f.project)["n"]
            == before
        )
