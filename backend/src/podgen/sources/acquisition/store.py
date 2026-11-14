"""Production project-audience journal over Repository and immutable runtime records."""

import io
from contextlib import contextmanager
from uuid import uuid5

from podgen.domain.contracts.common import CodeProducer, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.database import one, query

from .contracts import VERSION


class AcquisitionStore:
    def __init__(self, repo, project_id, operation_id):
        self.repo, self.project, self.operation = repo, project_id, operation_id
        self.completion, self.completed = None, None
        self.outcomes, self.texts = {}, {}
        self.initial = {}

    def start(self, request):
        """One SQL boundary owns request registration, config and recovery state."""
        from podgen.persistence.acquisition_reads import load
        from podgen.persistence.artifact_batch import publish
        from podgen.persistence.publication_plan import PublicationBatch, PublicationIdentities

        from .profile import load_profile

        keys = ["request", "result"] + [
            role + ":" + suffix
            for role in ("original", "mediawiki", "jina", "promoted_pdf", "browser")
            for suffix in ("outcome", "original", "source", "extracted", "representation")
        ]
        refs = {key: self.ref(key) for key in keys}
        raw = request.model_dump(mode="json")

        def initialize(tx):
            loaded = load(self.repo, self.project, tuple(refs.values()))
            records = {key: loaded.get(ref.record_id) for key, ref in refs.items()}
            if records["request"] is not None and records["request"] != raw:
                raise Conflict("source_acquisition_record_changed")
            if records["result"] is not None:
                return None, records
            if records["request"] is None:
                identities = PublicationIdentities(
                    tx,
                    (tx.get_transaction(), tx.get_nested_transaction()),
                    self.project,
                    frozenset(ref.record_id for ref in refs.values()),
                    {row["record_id"]: row for row in loaded.rows},
                )
                publish(
                    self.repo,
                    tx,
                    self.project,
                    PublicationBatch(runtimes=(self.node("request", raw),)),
                    identities=identities,
                )
            else:
                from podgen.persistence.acquisition_policy import JournalBinding, semantic_inputs
                from podgen.persistence.runtime_records import _validate_inputs

                required = semantic_inputs(JournalBinding(self.operation, "request"), raw)
                _validate_inputs(
                    self.repo, tx, self.project, [ref.model_dump(mode="json") for ref in required], "project"
                )
            request_row = next(
                (row for row in loaded.rows if row["record_id"] == self.ref("request").record_id), None
            )
            if request_row is not None and request_row["reference_policy"] == "legacy/1":
                from .legacy_recovery import decode

                records = decode(tx, self.project, self.operation, records)
            profile = load_profile(self.repo, self.project, request.profile_ref)
            return profile, records

        profile, records = archive_write(self.repo, self.project, initialize)
        self.initial = records
        return profile, records["result"]

    def stages(self, role, suffixes):
        keys = [role + ":" + suffix for suffix in suffixes]
        if all(key in self.initial for key in keys):
            return {key: self.initial.pop(key) for key in keys}
        return self.get_many(keys)

    def ref(self, key):
        return RuntimeRef(record_kind="acquisition_record", record_id=uuid5(self.operation, key))

    def get(self, key):
        return self.get_many([key])[key]

    def get_many(self, keys):
        if len(keys) > 64:
            raise ValueError("acquisition_read_batch_too_large")
        if not keys:
            return {}
        from podgen.persistence.acquisition_reads import load

        refs = {key: self.ref(key) for key in dict.fromkeys(keys)}
        found = load(self.repo, self.project, tuple(refs.values()))
        return {key: found.get(ref.record_id) for key, ref in refs.items()}

    def put(self, key, payload):
        return archive_write(self.repo, self.project, lambda tx: self._put(tx, key, payload))

    def _put(self, tx, key, payload):
        from podgen.persistence.artifact_batch import publish
        from podgen.persistence.publication_plan import PublicationBatch

        try:
            return publish(
                self.repo, tx, self.project, PublicationBatch(runtimes=(self.node(key, payload),))
            )[key]
        except Conflict as error:
            if error.code == "runtime_id_reused_with_different_input":
                raise Conflict("source_acquisition_record_changed") from error
            raise

    def node(self, key, payload):
        from podgen.persistence.acquisition_policy import JournalBinding
        from podgen.persistence.publication_plan import RuntimeNode

        parts = key.split(":", 1)
        binding = JournalBinding(self.operation, parts[-1], parts[0] if len(parts) == 2 else "")
        return RuntimeNode(key, self.ref(key), payload, journal=binding)

    def receipt(self, key, body, media_type, build):
        from podgen.persistence.blob_publication import publish_staged

        with self.repo.database.transaction() as tx:
            self.repo._acl(tx, self.project, write=True)
        with self.repo.objects.stage(self.project, io.BytesIO(body), media_type) as staged:
            return publish_staged(
                self.repo, self.project, [staged], publish=lambda tx, refs: self._put(tx, key, build(refs[0]))
            )

    def bytes(self, key, blob):
        return self.repo.read_runtime_blob(self.project, self.ref(key), blob)

    def producer(self, key):
        return CodeProducer(
            kind="code",
            component="source-acquisition",
            build_id=VERSION,
            operation_id=uuid5(self.operation, key),
        )

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
