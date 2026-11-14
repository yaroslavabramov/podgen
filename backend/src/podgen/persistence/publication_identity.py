"""Recover existing IDs before resolving any dependent immutable payload."""

from uuid import UUID

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import Conflict, ContractError

from .database import bounded_rows


def resolve(tx, project, batch, identities=None):
    previous = (
        {
            row["operation_id"]: row
            for row in bounded_rows(
                tx,
                "SELECT * FROM artifact_revisions WHERE project_id=:p AND operation_id=ANY(:ids)",
                [node.operation_id for node in batch.artifacts],
                p=project,
            )
        }
        if batch.artifacts
        else {}
    )
    if identities is not None:
        runtime_rows = identities.require(tx, project, [node.ref.record_id for node in batch.runtimes])
    else:
        runtime_rows = (
            {
                row["record_id"]: row
                for row in bounded_rows(
                    tx,
                    "SELECT * FROM runtime_records WHERE project_id=:p AND record_id=ANY(:ids)",
                    [node.ref.record_id for node in batch.runtimes],
                    p=project,
                )
            }
            if batch.runtimes
            else {}
        )
    artifacts = dict(batch.external_artifacts)
    runtimes = batch.external_runtimes | {node.key: node.ref for node in batch.runtimes}
    pending = list(batch.artifacts)
    while pending:
        ready = [node for node in pending if node.identity_of is None or node.identity_of in artifacts]
        if not ready:
            raise ContractError("cyclic_publication_identity")
        for node in ready:
            old = previous.get(node.operation_id)
            body = node.payload if node.kind == "SourceRevision" else None
            identity = (
                UUID(body["source_id"])
                if body is not None
                else (artifacts[node.identity_of].artifact_id if node.identity_of else node.artifact_id)
            )
            if body is not None and (
                node.artifact_id is not None
                and identity != node.artifact_id
                or node.identity_of
                and identity != artifacts[node.identity_of].artifact_id
            ):
                raise ContractError("source_identity_mismatch")
            if old is not None:
                if old["kind"] != node.kind:
                    raise Conflict("operation_id_reused_with_different_input")
                artifacts[node.key] = ArtifactRef(
                    **{key: old[key] for key in ("artifact_id", "revision_id", "kind")}
                )
            else:
                artifacts[node.key] = ArtifactRef(
                    artifact_id=identity or node.allocated_artifact_id,
                    revision_id=node.revision_id,
                    kind=node.kind,
                )
            pending.remove(node)
    return previous, runtime_rows, artifacts, runtimes
