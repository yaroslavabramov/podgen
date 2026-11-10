"""Live source authorization and withdrawal serialization for project-local retrieval projections."""

from contextlib import contextmanager

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.domain.references import walk_values
from podgen.persistence.database import one, query
from podgen.persistence.read_authorization import authorized_read


class IndexAccess:
    def __init__(self, journal, shared_input_ref):
        self.journal, self.reference = journal, shared_input_ref
        self.source_hashes = set()

    @contextmanager
    def transaction(self):
        journal = self.journal
        with journal.repo.database.transaction() as tx:
            # Same ordering as immutable archive publication and supported content withdrawal.
            query(tx, "SELECT project_id FROM projects WHERE project_id=:p FOR SHARE", p=journal.project)
            query(
                tx,
                "SELECT role FROM project_members WHERE project_id=:p AND actor_id=:a FOR SHARE",
                p=journal.project,
                a=journal.repo.actor_id,
            )
            with authorized_read(journal.repo, tx):
                journal.authorize(tx)
                hashes = set(self.source_hashes)
                seen = set()

                def visit(reference):
                    identity = reference.model_dump_json()
                    if identity in seen:
                        return
                    seen.add(identity)
                    if isinstance(reference, BlobRef):
                        journal.repo._check_blob(tx, journal.project, reference)
                        hashes.add(reference.sha256)
                        return
                    row = (
                        journal.repo._runtime(tx, journal.project, reference)
                        if isinstance(reference, RuntimeRef)
                        else journal.repo._resolve(tx, journal.project, reference)
                    )
                    for _, _, linked in walk_values(row["payload"]):
                        if isinstance(linked, (ArtifactRef, BlobRef, RuntimeRef)):
                            visit(linked)

                visit(self.reference)
                if hashes and one(
                    tx,
                    "SELECT 1 FROM blob_access_revocations WHERE project_id=:p AND sha256=ANY(:hashes)",
                    p=journal.project,
                    hashes=sorted(hashes),
                ):
                    raise AccessDenied()
                yield tx, hashes
