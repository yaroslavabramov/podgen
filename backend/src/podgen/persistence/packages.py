from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.domain.packages import PackageArtifact, package_nodes, relative_path, validate_package

from .database import json_text, one, query


class PackageImporter:
    def __init__(self, repository):
        self.repo = repository

    def import_package(self, project_id, episode_id, package, root, *, producer):
        repo = self.repo
        validate_package(package, root)
        refs = dict(package.external_refs)
        runtimes = dict(package.external_runtime_refs)
        blobs = {}
        for blob in package.blobs:
            with relative_path(root, blob.path).open("rb") as stream:
                blobs[blob.key] = repo.save_blob(
                    project_id,
                    stream,
                    blob.media_type,
                    expected_sha256=blob.sha256,
                    expected_length=blob.byte_length,
                )
        saved = []
        from .artifact_preparation import prepare_package

        prepared = prepare_package(repo, project_id, package, refs, runtimes, blobs, root)
        with repo.database.transaction() as tx:
            repo._acl(tx, project_id, write=True)
            repo._lock(tx, project_id)
            from .artifact_batch import publish
            from .publication_plan import ArtifactNode, PublicationBatch, RuntimeNode

            artifact_nodes = tuple(
                ArtifactNode(
                    item.key,
                    item.kind,
                    item.payload,
                    producer,
                    uuid5(package.package_id, "operation:" + item.key),
                    input_keys=tuple(item.input_keys),
                    identity_of=item.identity_of,
                    episode_id=episode_id,
                    audience=item.audience,
                    streaming=True,
                )
                for item in package.artifacts
            )
            runtime_nodes = tuple(
                RuntimeNode(
                    item.key,
                    RuntimeRef(
                        record_kind=item.record_kind,
                        record_id=uuid5(package.package_id, "runtime:" + item.key),
                    ),
                    item.payload,
                    audience=item.audience,
                )
                for item in package.runtime_records
            )
            values = publish(
                repo,
                tx,
                project_id,
                PublicationBatch(
                    artifacts=artifact_nodes,
                    runtimes=runtime_nodes,
                    external_artifacts=refs,
                    external_runtimes=runtimes,
                    blobs=blobs,
                    prepared=prepared,
                    root=root,
                ),
            )
            saved = [values[item.key] for item in package_nodes(package) if isinstance(item, PackageArtifact)]
            runtimes.update({item.key: values[item.key] for item in package.runtime_records})
            provenance = dict(
                test_data=package.test_data,
                description=package.description,
                output_refs=[r.ref.model_dump(mode="json") for r in saved],
                runtime_refs=[r.model_dump(mode="json") for r in runtimes.values()],
            )
            previous = one(
                tx,
                "SELECT payload FROM operational_records WHERE project_id=:p AND record_kind='artifact_package' AND record_id=:id",
                p=project_id,
                id=package.package_id,
            )
            if previous and previous["payload"] != provenance:
                raise ContractError("package_id_reused_with_different_provenance")
            if not previous:
                query(
                    tx,
                    "INSERT INTO operational_records VALUES(:p,'artifact_package',:id,NULL,CAST(:body AS json))",
                    p=project_id,
                    id=package.package_id,
                    body=json_text(provenance),
                )
        return saved
