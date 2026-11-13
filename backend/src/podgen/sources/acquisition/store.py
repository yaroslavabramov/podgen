"""Production project-audience journal over Repository and immutable runtime records."""

import io
from contextlib import contextmanager
from uuid import uuid5

from podgen.domain.contracts.common import CodeProducer, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query

from .contracts import VERSION


class AcquisitionStore:
    def __init__(self, repo, project_id, operation_id):
        self.repo, self.project, self.operation = repo, project_id, operation_id

    def ref(self, key):
        return RuntimeRef(record_kind="acquisition_record", record_id=uuid5(self.operation, key))

    def get(self, key):
        ref = self.ref(key)
        with self.repo.database.read_transaction() as tx:
            self.repo._acl(tx, self.project)
            found = one(
                tx,
                "SELECT record_id FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=self.project,
                r=ref.record_id,
            )
        return self.repo.get_runtime(self.project, ref) if found else None

    def put(self, key, payload):
        existing = self.get(key)
        if existing is not None:
            if existing != payload:
                raise Conflict("source_acquisition_record_changed")
            return self.ref(key)
        return self.repo.append_runtime(self.project, self.ref(key), payload)

    def blob(self, body, media):
        return self.repo.save_blob(self.project, io.BytesIO(body), media)

    def bytes(self, key, blob):
        return self.repo.read_runtime_blob(self.project, self.ref(key), blob)

    def create(self, key, kind, payload, *, artifact_id=None):
        operation = uuid5(self.operation, key)
        return self.repo.create(
            self.project,
            kind,
            payload,
            producer=CodeProducer(
                kind="code", component="source-acquisition", build_id=VERSION, operation_id=operation
            ),
            operation_id=operation,
            artifact_id=artifact_id,
        ).ref

    @contextmanager
    def lock(self):
        with self.repo.database.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            self.repo._acl(connection, self.project, write=True)
            key = f"source-acquisition:{self.project}:{self.operation}"
            held = one(connection, "SELECT pg_try_advisory_lock(hashtextextended(:key,0)) AS held", key=key)[
                "held"
            ]
            if not held:
                raise Conflict("source_acquisition_busy")
            try:
                yield
            finally:
                query(connection, "SELECT pg_advisory_unlock(hashtextextended(:key,0))", key=key)
