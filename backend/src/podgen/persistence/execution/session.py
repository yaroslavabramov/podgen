"""Run-owned execution storage. Cached descriptors never replace live authorization."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.references import walk_values
from podgen.persistence.database import one

from .inputs import _publish, bind
from .objects import OwnedBlob
from .store import append


class ExecutionStore:
    def __init__(self, repo, project):
        self.repo, self.project = repo, project
        self.sets, self.roots, self.requests = {}, {}, set()

    def prepare(self, run_id, *, evaluation=(), generation=()):
        roots = [
            dict(purpose=purpose, ref=ref.model_dump(mode="json"))
            for purpose, references in (("evaluation", evaluation), ("generation", generation))
            for ref in references
        ]
        known = self.roots.setdefault(run_id, set())
        incoming = {digest(root) for root in roots}
        if run_id not in self.sets or not incoming.issubset(known):
            if not roots:
                with self.repo.database.read_transaction() as tx:
                    row = one(
                        tx,
                        "SELECT manifest_id FROM evaluation_writing_runs WHERE project_id=:p AND run_id=:r",
                        p=self.project,
                        r=run_id,
                    )
                roots = [
                    dict(
                        purpose="evaluation",
                        ref=RuntimeRef(
                            record_kind="context_assembly", record_id=row["manifest_id"]
                        ).model_dump(mode="json"),
                    )
                ]
            self.sets[run_id] = bind(self.repo, self.project, run_id, roots)
            known.update(digest(root) for root in self.sets[run_id]["roots"])
        return self.sets[run_id]

    def invocation(self, invocation):
        run_id = invocation["run_id"]
        if run_id not in self.sets:
            with self.repo.database.read_transaction() as tx:
                row = one(
                    tx,
                    "SELECT * FROM execution_input_sets WHERE project_id=:p AND run_id=:r ORDER BY generation DESC LIMIT 1",
                    p=self.project,
                    r=run_id,
                )
            if row:
                self.sets[run_id] = dict(row)
                self.roots[run_id] = {digest(root) for root in row["roots"]}
            else:
                self.prepare(
                    run_id,
                    evaluation=[
                        RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
                    ],
                )
        request_id = invocation["request_record_id"]
        if request_id not in self.requests:
            with self.repo.database.read_transaction() as tx:
                request = one(
                    tx,
                    "SELECT execution_input_set_id FROM runtime_records WHERE project_id=:p AND record_id=:id",
                    p=self.project,
                    id=request_id,
                )
            if request is not None and request["execution_input_set_id"] is None:
                self.prepare(
                    run_id, evaluation=[RuntimeRef(record_kind="tool_execution", record_id=request_id)]
                )
            self.requests.add(request_id)
        return self.sets[run_id]

    def prepare_context(self, run_id, context):
        return self.prepare(run_id, evaluation=[ref for _, _, ref in walk_values(context)])

    def append(self, tx, run_id, records, blobs):
        row = self.sets[run_id]
        append(self.repo, tx, self.project, run_id, row["set_id"], records, blobs)

    def publish(self, operation):
        return _publish(self.repo, self.project, operation)

    @staticmethod
    def owned(values):
        return [OwnedBlob(ref, body) for (body, _), ref in values.items()]
