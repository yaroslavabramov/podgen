from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.domain.packages import PackageRuntime, package_nodes, relative_path, substitute, validate_package

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
        with repo.database.transaction() as tx:
            repo._acl(tx, project_id, write=True)
            repo._lock(tx, project_id)
            for ref in refs.values():
                repo._resolve(tx, project_id, ref)
            for ref in runtimes.values():
                repo._runtime(tx, project_id, ref)
            for item in package_nodes(package):
                body = substitute(item.payload, refs, runtimes, blobs, root)
                if isinstance(item, PackageRuntime):
                    from .runtime_records import append_runtime

                    ref = RuntimeRef(
                        record_kind=item.record_kind,
                        record_id=uuid5(package.package_id, "runtime:" + item.key),
                    )
                    append_runtime(repo, tx, project_id, ref, body, item.audience, replay=True)
                    runtimes[item.key] = ref
                else:
                    revision = repo._create(
                        tx,
                        project_id,
                        item.kind,
                        body,
                        producer=producer,
                        operation_id=uuid5(package.package_id, "operation:" + item.key),
                        episode_id=episode_id,
                        artifact_id=refs[item.identity_of].artifact_id if item.identity_of else None,
                        input_refs=[refs[key] for key in item.input_keys],
                        audience=item.audience,
                        streaming=True,
                    )
                    refs[item.key] = revision.ref
                    saved.append(revision)
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
