"""Audio state is derived from immutable WP-01 records; no writing-run impersonation."""

from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.database import query
from podgen.persistence.read_authorization import _read_connection, read_operation

from ..journal import EvaluationJournal


class AudioJournal:
    def __init__(self, repo, project, run_id):
        self.base = EvaluationJournal(repo, project)
        self.repo, self.project, self.run_id = repo, project, run_id

    def ref(self, key):
        return RuntimeRef(record_kind="tool_execution", record_id=uuid5(self.run_id, "audio:" + key))

    def get(self, key):
        return self.get_many([key])[0]

    def read_operation(self):
        return self.base.read_operation()

    def get_many(self, keys):
        keys = tuple(keys)
        if len(keys) > 64:
            raise ContractError("audio_read_batch_too_large")
        references = [self.ref(key) for key in keys]
        with read_operation(self.repo):
            with _read_connection(self.repo) as tx:
                self.base.authorize(tx)
                present = set(
                    query(
                        tx,
                        """SELECT record_id FROM runtime_records
                    WHERE project_id=:p AND record_id=ANY(CAST(:ids AS uuid[]))""",
                        p=self.project,
                        ids=[ref.record_id for ref in references],
                    ).scalars()
                )
            found = [ref for ref in dict.fromkeys(references) if ref.record_id in present]
            values = self.repo.get_runtime_many(self.project, found) if found else []
            by_id = {ref.record_id: value for ref, value in zip(found, values, strict=True)}
            return [by_id.get(ref.record_id) for ref in references]

    def put(self, key, value):
        # The immutable append already compares an existing record atomically.
        return self.base.record(value, record_id=self.ref(key).record_id)

    def attempt_keys(self, cells):
        from .attempts import prefix

        result = {cell["cell_id"]: [] for cell in cells}
        sources = [RuntimeRef.model_validate(cell["source_ref"]).record_id for cell in cells]
        with _read_connection(self.repo) as tx:
            self.base.authorize(tx)
            rows = list(
                query(
                    tx,
                    """SELECT DISTINCT r.record_id,r.payload->>'cell_id' AS cell_id,
                (r.payload->>'take')::integer AS take
                FROM runtime_runtime_refs edge JOIN runtime_records r USING(project_id,record_id)
                WHERE edge.project_id=:p AND edge.target_record_id=ANY(CAST(:sources AS uuid[]))
                  AND r.payload->>'contract'='audio-attempt/1'
                  AND r.payload->>'cell_id'=ANY(CAST(:cells AS text[]))
                ORDER BY cell_id,take""",
                    p=self.project,
                    sources=sources,
                    cells=list(result),
                ).mappings()
            )
        for row in rows:
            key = prefix(row["cell_id"], row["take"])
            if row["take"] != len(result[row["cell_id"]]) + 1:
                raise ContractError("audio_attempt_sequence_mismatch")
            if row["record_id"] != self.ref(key + ":prepared").record_id:
                raise ContractError("audio_attempt_archive_binding_mismatch")
            result[row["cell_id"]].append(key)
        return result

    def processing_keys(self, attempt_keys):
        receipts = {self.ref(key + ":receipt").record_id: key for key in attempt_keys}
        grouped = {key: set() for key in attempt_keys}
        with _read_connection(self.repo) as tx:
            self.base.authorize(tx)
            rows = list(
                query(
                    tx,
                    """SELECT edge.target_record_id,r.record_id
                FROM runtime_runtime_refs edge JOIN runtime_records r USING(project_id,record_id)
                WHERE edge.project_id=:p AND edge.target_record_id=ANY(CAST(:receipts AS uuid[]))
                  AND r.payload->>'contract'='audio-processing-attempt/1'""",
                    p=self.project,
                    receipts=list(receipts),
                ).mappings()
            )
        for row in rows:
            grouped[receipts[row["target_record_id"]]].add(row["record_id"])
        result = {}
        for key, identities in grouped.items():
            keys = [key, *[f"{key}:reprocess:{index}" for index in range(1, len(identities))]]
            if identities and {self.ref(value + ":processing").record_id for value in keys} != identities:
                raise ContractError("audio_processing_archive_binding_mismatch")
            result[key] = keys
        return result

    def blob(self, body, media):
        return self.base.blob(body, media).model_dump(mode="json")

    def bytes(self, key, blob):
        from podgen.domain.contracts.common import BlobRef

        return self.base.read_blob(self.ref(key), BlobRef.model_validate(blob))

    def lock(self):
        return self.base.lock(uuid5(self.run_id, "audio-comparison-lock"))
