"""Check declared source identities against prepared flat metadata, without graph walks."""

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.domain.references import walk_values
from podgen.persistence.database import query


def validate(tx, project, command, parents):
    private = {
        view["record_id"]: view["record_kind"] for row in parents.values() for view in row["archive_views"]
    }
    private.update({view["record_id"]: view["record_kind"] for view in command.views})
    owned = {obj.reference for obj in command.objects.values()}
    pending = set()
    for view in command.views:
        for _, _, ref in walk_values(view["payload"]):
            if isinstance(ref, BlobRef) and ref in owned:
                continue
            if isinstance(ref, RuntimeRef) and str(ref.record_id) in private:
                if private[str(ref.record_id)] != ref.record_kind:
                    raise ContractError("private_reference_kind_mismatch")
                continue
            pending.add(ref)
    values = list(pending)
    for offset in range(0, len(values), 128):
        batch = values[offset : offset + 128]
        identities = [
            str(ref.revision_id)
            if isinstance(ref, ArtifactRef)
            else str(ref.record_id)
            if isinstance(ref, RuntimeRef)
            else ref.storage_key
            for ref in batch
        ]
        rows = list(
            query(
                tx,
                """SELECT resource_kind,identity,binding FROM execution_input_resources
            WHERE project_id=:p AND set_id=:s AND identity=ANY(CAST(:ids AS text[]))
            AND resource_kind IN ('artifact','runtime','blob')""",
                p=project,
                s=command.input_set_id,
                ids=identities,
            ).mappings()
        )
        found = {(row["resource_kind"], row["identity"]): row["binding"] for row in rows}
        for ref, identity in zip(batch, identities, strict=True):
            kind = (
                "artifact"
                if isinstance(ref, ArtifactRef)
                else "runtime"
                if isinstance(ref, RuntimeRef)
                else "blob"
            )
            if found.get((kind, identity)) != ref.model_dump(mode="json"):
                raise ContractError("unbound_private_checkpoint_source")
