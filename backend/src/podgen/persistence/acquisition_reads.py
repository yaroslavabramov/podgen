"""Direct bounded journal reads; technical control does not load its parent graph."""

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import AccessDenied
from podgen.domain.references import walk_values

from .acquisition_policy import VERSION
from .blob_access import validate_identity
from .database import query
from .read_authorization import _read_connection, read_operation


def load(repo, project, references):
    """Legacy records retain full reads. New receipts check only their own data."""
    if not references:
        return {}
    with read_operation(repo):
        with _read_connection(repo) as tx:
            repo._acl(tx, project)
            rows = list(
                query(
                    tx,
                    """SELECT r.*,
                EXISTS(SELECT 1 FROM runtime_blob_refs b JOIN blob_access_revocations v
                    USING(project_id,sha256) WHERE b.project_id=r.project_id AND b.record_id=r.record_id)
                OR EXISTS(SELECT 1 FROM execution_record_withdrawals w
                    WHERE w.project_id=r.project_id AND w.record_id=r.record_id) AS denied,
                EXISTS(SELECT 1 FROM runtime_blob_refs b JOIN protected_byte_hashes h
                    USING(project_id,sha256) WHERE b.project_id=r.project_id AND b.record_id=r.record_id) AS protected
                FROM runtime_records r WHERE project_id=:p AND record_id=ANY(:ids)""",
                    p=project,
                    ids=[ref.record_id for ref in references],
                ).mappings()
            )
            result, legacy, outputs = {}, [], []
            for row in rows:
                if row["reference_policy"] != VERSION:
                    legacy.append(next(ref for ref in references if ref.record_id == row["record_id"]))
                    continue
                validate_identity(
                    evaluator_only=row["protected"],
                    access_revoked=row["denied"],
                    purpose=repo.purpose,
                    role=repo._acl(tx, project),
                )
                if row["audience"] != "project" or row["record_kind"] != "acquisition_record":
                    raise AccessDenied()
                result[row["record_id"]] = row["payload"]
                if row["journal_binding"]["stage"] in ("outcome", "result"):
                    outputs.extend(
                        ref for _, _, ref in walk_values(row["payload"]) if isinstance(ref, ArtifactRef)
                    )
            if outputs:
                from .runtime_records import _validate_inputs

                _validate_inputs(
                    repo, tx, project, [ref.model_dump(mode="json") for ref in outputs], "project"
                )
        if legacy:
            result.update(
                zip((ref.record_id for ref in legacy), repo.get_runtime_many(project, legacy), strict=True)
            )
        return JournalRead(result, rows)


class JournalRead(dict):
    def __init__(self, payloads, rows):
        super().__init__(payloads)
        self.rows = rows
