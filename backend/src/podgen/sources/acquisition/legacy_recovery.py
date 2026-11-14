"""Decode old operation rows into recovery inputs, without rewriting any history."""

from uuid import uuid5

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import ContractError
from podgen.persistence.database import bounded_rows

ROLES = ("original", "mediawiki", "jina", "promoted_pdf", "browser")


def decode(tx, project, operation, records):
    names = {
        uuid5(operation, role + ":" + stage): (role, stage)
        for role in ROLES
        for stage in ("source", "metadata-source", "representation")
    }
    rows = list(
        bounded_rows(
            tx,
            "SELECT * FROM artifact_revisions WHERE project_id=:p AND operation_id=ANY(:ids)",
            names,
            p=project,
        )
    )
    found = {names[row["operation_id"]]: row for row in rows}
    for role in ROLES:
        source = found.get((role, "metadata-source")) or found.get((role, "source"))
        if source is not None:
            ref = ArtifactRef(**{key: source[key] for key in ("artifact_id", "revision_id", "kind")})
            if ref.kind != "SourceRevision":
                raise ContractError("source_acquisition_record_changed")
            records[role + ":source"] = dict(source_revision_ref=ref.model_dump(mode="json"))
        representation = found.get((role, "representation"))
        if representation is not None and records[role + ":representation"] is None:
            ref = ArtifactRef(**{key: representation[key] for key in ("artifact_id", "revision_id", "kind")})
            if ref.kind != "SourceRepresentation":
                raise ContractError("source_acquisition_record_changed")
            records[role + ":representation"] = dict(representation_ref=ref.model_dump(mode="json"))
    return records
