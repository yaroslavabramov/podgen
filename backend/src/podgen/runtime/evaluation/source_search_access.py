"""Live source authorization and withdrawal serialization for project-local retrieval projections."""

from contextlib import contextmanager

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.access_facts import access_facts
from podgen.persistence.database import one, query
from podgen.persistence.read_authorization import authorized_read


class IndexAccess:
    def __init__(self, journal, shared_input_ref):
        self.journal, self.reference = journal, shared_input_ref
        self.source_hashes = set()
        self.admission_references = []

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
                facts = access_facts(journal.repo, tx)
                runtime_roots = [
                    ref for ref in [self.reference, *self.admission_references] if isinstance(ref, RuntimeRef)
                ]
                facts.runtimes.prefetch(journal.project, runtime_roots)
                hashes = set(self.source_hashes)
                seen = set()

                def visit(reference):
                    identity = journal.project, reference.model_dump_json()
                    if identity in seen:
                        return
                    seen.add(identity)
                    if isinstance(reference, BlobRef):
                        facts.blobs.check(journal.project, reference)
                        hashes.add(reference.sha256)
                        return
                    if isinstance(reference, RuntimeRef):
                        journal.repo._runtime(tx, journal.project, reference)
                        references = facts.runtimes.references[journal.project, reference.record_id]
                    else:
                        row = journal.repo._authorize_artifact(tx, journal.project, reference)
                        if row["project_id"] != journal.project:
                            # Catalog configurations are fully authorized by
                            # ScopeAccess, including their owner-scoped blobs and
                            # publication grants. Their bytes are not local source
                            # text and cannot enter a tenant's purge registry.
                            return
                        references = facts.artifacts.access_references(row)
                    for linked in references:
                        visit(linked)

                visit(self.reference)
                for reference in self.admission_references:
                    visit(reference)
                if hashes and one(
                    tx,
                    "SELECT 1 FROM blob_access_revocations WHERE project_id=:p AND sha256=ANY(:hashes)",
                    p=journal.project,
                    hashes=sorted(hashes),
                ):
                    raise AccessDenied()
                yield tx, hashes
