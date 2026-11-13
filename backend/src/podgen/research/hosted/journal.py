"""Project-visible immutable research records in the production repository."""

import io
from contextlib import contextmanager
from uuid import uuid5

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query
from podgen.persistence.runtime_records import append_runtime


class ResearchJournal:
    def __init__(self, repo, project, run_id):
        self.repo, self.project, self.run_id = repo, project, run_id

    def ref(self, key):
        return RuntimeRef(
            record_kind="tool_execution", record_id=uuid5(self.run_id, "hosted-research:" + key)
        )

    def get(self, key):
        ref = self.ref(key)
        from podgen.persistence.read_authorization import _read_connection, read_operation

        with read_operation(self.repo):
            with _read_connection(self.repo) as tx:
                self.repo._acl(tx, self.project)
                found = one(
                    tx,
                    "SELECT record_id FROM runtime_records WHERE project_id=:p AND record_id=:r",
                    p=self.project,
                    r=ref.record_id,
                )
            return self.repo.get_runtime(self.project, ref) if found else None

    def put(self, key, value):
        with self.repo.database.transaction() as tx:
            append_runtime(self.repo, tx, self.project, self.ref(key), value, "project", replay=True)
        return self.ref(key)

    def blob(self, body, media="application/json"):
        return self.repo.save_blob(self.project, io.BytesIO(body), media).model_dump(mode="json")

    def bytes(self, key, blob):
        return self.repo.read_runtime_blob(self.project, self.ref(key), BlobRef.model_validate(blob))

    @contextmanager
    def lock(self):
        with self.repo.database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as tx:
            self.repo._acl(tx, self.project, write=True)
            key = f"hosted-research:{self.project}:{self.run_id}"
            if not one(tx, "SELECT pg_try_advisory_lock(hashtextextended(:key,0)) AS held", key=key)["held"]:
                raise Conflict("hosted_research_run_busy")
            try:
                yield
            finally:
                query(tx, "SELECT pg_advisory_unlock(hashtextextended(:key,0))", key=key)
