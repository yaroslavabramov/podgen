"""Production project-audience journal over Repository and immutable runtime records."""

import io
from contextlib import contextmanager
from uuid import uuid5

from podgen.domain.contracts.common import CodeProducer, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.database import one, query
from podgen.persistence.read_authorization import _read_connection, read_operation
from podgen.persistence.runtime_records import append_runtime

from .contracts import VERSION


class AcquisitionStore:
    def __init__(self, repo, project_id, operation_id):
        self.repo, self.project, self.operation = repo, project_id, operation_id

    def ref(self, key):
        return RuntimeRef(record_kind="acquisition_record", record_id=uuid5(self.operation, key))

    def get(self, key):
        return self.get_many([key])[key]

    def get_many(self, keys):
        if len(keys) > 64:
            raise ValueError("acquisition_read_batch_too_large")
        if not keys:
            return {}
        refs = {key: self.ref(key) for key in dict.fromkeys(keys)}
        with read_operation(self.repo):
            with _read_connection(self.repo) as tx:
                self.repo._acl(tx, self.project)
                found = set(
                    query(
                        tx,
                        "SELECT record_id FROM runtime_records WHERE project_id=:p AND record_id=ANY(:ids)",
                        p=self.project,
                        ids=[ref.record_id for ref in refs.values()],
                    ).scalars()
                )
            selected = [(key, ref) for key, ref in refs.items() if ref.record_id in found]
            values = self.repo.get_runtime_many(self.project, [ref for _, ref in selected])
            return dict.fromkeys(keys) | dict(zip((key for key, _ in selected), values, strict=True))

    def put(self, key, payload):
        return archive_write(self.repo, self.project, lambda tx: self._put(tx, key, payload))

    def _put(self, tx, key, payload):
        try:
            return append_runtime(self.repo, tx, self.project, self.ref(key), payload, replay=True)
        except Conflict as error:
            if error.code == "runtime_id_reused_with_different_input":
                raise Conflict("source_acquisition_record_changed") from error
            raise

    def blob(self, body, media):
        return self.repo.save_blob(self.project, io.BytesIO(body), media)

    def bytes(self, key, blob):
        return self.repo.read_runtime_blob(self.project, self.ref(key), blob)

    def create(self, key, kind, payload, *, artifact_id=None):
        from podgen.persistence.artifact_preparation import prepare

        prepared = prepare(self.repo, self.project, kind, payload)
        return self.publication(
            lambda publication: publication.create(key, kind, payload, artifact_id=artifact_id).ref,
            prepared=prepared,
        )

    def publication(self, publish, *, prepared=None):
        # The boundary retries only rolled-back SQL. Prepared bytes remain owned
        # by the caller; returned descriptors escape only after commit succeeds.
        return archive_write(self.repo, self.project, lambda tx: publish(Publication(self, tx, prepared)))

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


class Publication:
    """One document transition; artifact and checkpoint use existing writers."""

    def __init__(self, store, tx, prepared):
        self.store, self.tx, self.prepared = store, tx, prepared

    def put(self, key, payload):
        return self.store._put(self.tx, key, payload)

    def create(self, key, kind, payload, *, artifact_id=None):
        store = self.store
        operation = uuid5(store.operation, key)
        return store.repo._create(
            self.tx,
            store.project,
            kind,
            payload,
            producer=CodeProducer(
                kind="code", component="source-acquisition", build_id=VERSION, operation_id=operation
            ),
            operation_id=operation,
            artifact_id=artifact_id,
            prepared=self.prepared,
        )
