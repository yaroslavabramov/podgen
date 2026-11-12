"""Audio state is derived from immutable WP-01 records; no writing-run impersonation."""

from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one

from ..journal import EvaluationJournal


class AudioJournal:
    def __init__(self, repo, project, run_id):
        self.base = EvaluationJournal(repo, project)
        self.repo, self.project, self.run_id = repo, project, run_id

    def ref(self, key):
        return RuntimeRef(record_kind="tool_execution", record_id=uuid5(self.run_id, "audio:" + key))

    def get(self, key):
        ref = self.ref(key)
        with self.repo.database.read_transaction() as tx:
            self.base.authorize(tx)
            found = one(
                tx,
                "SELECT record_id FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=self.project,
                r=ref.record_id,
            )
        return self.base.read(ref) if found else None

    def put(self, key, value):
        previous = self.get(key)
        if previous is not None:
            if previous != value:
                raise Conflict("audio_record_reused_with_different_input")
            return self.ref(key)
        return self.base.record(value, record_id=self.ref(key).record_id)

    def blob(self, body, media):
        return self.base.blob(body, media).model_dump(mode="json")

    def bytes(self, key, blob):
        from podgen.domain.contracts.common import BlobRef

        return self.base.read_blob(self.ref(key), BlobRef.model_validate(blob))

    def lock(self):
        return self.base.lock(uuid5(self.run_id, "audio-comparison-lock"))
