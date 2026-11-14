"""One blob-access decision for scalar storage checks and operation graph facts."""

from podgen.domain.errors import AccessDenied


def validate_identity(*, evaluator_only, access_revoked, purpose, role, public_only=False):
    if evaluator_only and (
        public_only or purpose != "evaluation" or role not in ("owner", "evaluator", "operator")
    ):
        raise AccessDenied()
    if access_revoked:
        raise AccessDenied()


def validate_binding(reference, row):
    if row is None or (row["sha256"], row["byte_length"], row["media_type"]) != (
        reference.sha256,
        reference.byte_length,
        reference.media_type,
    ):
        raise AccessDenied()


class BlobFacts:
    """Exact byte bindings loaded by metadata frontier, with one access decision."""

    def __init__(self, repo, tx, role, *, publication=None):
        self.repo, self.tx, self.role, self.rows = repo, tx, role, {}
        self.publication = publication

    def load(self, project, keys):
        keys = tuple(keys)
        from .database import query

        if self.publication is not None:
            self.publication.sync()
        pending = [key for key in set(keys) if (project, key) not in self.rows]
        for offset in range(0, len(pending), 64):
            for row in query(
                self.tx,
                """SELECT b.*,
                EXISTS(SELECT 1 FROM blob_access_revocations v WHERE v.project_id=b.project_id AND v.sha256=b.sha256) AS access_revoked,
                EXISTS(SELECT 1 FROM protected_byte_hashes c WHERE c.project_id=b.project_id AND c.sha256=b.sha256) AS evaluator_only
                FROM blobs b WHERE project_id=:p AND storage_key=ANY(:keys)""",
                p=project,
                keys=pending[offset : offset + 64],
            ).mappings():
                self.rows[row["project_id"], row["storage_key"]] = row

    def check(self, project, reference, *, public_only=False):
        self.load(project, [reference.storage_key])
        row = self.rows.get((project, reference.storage_key))
        validate_binding(reference, row)
        role = (
            (self.role(project) or {}).get("role")
            if row["evaluator_only"] and self.repo.purpose == "evaluation" and not public_only
            else None
        )
        validate_identity(
            evaluator_only=row["evaluator_only"],
            access_revoked=row["access_revoked"],
            purpose=self.repo.purpose,
            role=role,
            public_only=public_only,
        )
