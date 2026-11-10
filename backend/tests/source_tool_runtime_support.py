"""In-memory persistence boundary for real gateway and provider protocol tests."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.runtime.evaluation import model_turns


class MemoryJournal:
    def __init__(self, manifest, body):
        self.snapshot = manifest
        self.records, self.blobs, self.receipts, self.turns = {}, {}, [], []
        self.read_count = 0
        self.root = dict(
            invocation_id=uuid4(),
            run_id=uuid4(),
            stage="draft",
            operation_kind="role",
            status="prepared",
            attempt_index=0,
            attempt_kind="role",
            parent_invocation_id=None,
            input_tokens=None,
            output_tokens=None,
            provider_response_id=None,
            output_record_id=None,
        )
        self.root["request_record_id"] = self.store_request(body)
        self.roots = {self.root["invocation_id"]: self.root}
        self.repo = SimpleNamespace(actor_id=uuid4())

    def blob(self, data):
        import hashlib

        digest = hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return BlobRef(
            storage_key=digest, sha256=digest, byte_length=len(data), media_type="application/json"
        )

    def store_request(self, body):
        import json

        ref = uuid4()
        self.records[ref] = dict(
            request_blob=self.blob(json.dumps(body).encode()).model_dump(mode="json"), context={}
        )
        return ref

    def read(self, ref):
        self.read_count += 1
        return deepcopy(self.records[ref.record_id])

    def read_blob(self, ref, blob):
        return self.blobs[blob.sha256]

    def request(self, row):
        import json

        return json.loads(
            self.read_blob(
                None, BlobRef.model_validate(self.records[row["request_record_id"]]["request_blob"])
            )
        )

    def manifest(self, run_id):
        return self.snapshot

    def events(self, root):
        return [event for event in self.receipts if event["invocation_id"] == root["invocation_id"]]

    def event(self, root, kind, *, reply=None, data=None):
        ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        record = dict(
            event_kind=kind,
            data=deepcopy(data or {}),
            invocation_id=str(root["invocation_id"]),
            request_ref=RuntimeRef(
                record_kind="tool_execution", record_id=root["request_record_id"]
            ).model_dump(mode="json"),
        )
        if reply:
            record["response_blob"] = self.blob(reply.body).model_dump(mode="json")
            record["provider_http_status"] = reply.status_code
        self.records[ref.record_id] = record
        self.receipts.append(
            dict(record_id=ref.record_id, event_kind=kind, invocation_id=root["invocation_id"])
        )
        return ref

    def transition(self, row, status, **kwargs):
        if row["invocation_id"] in self.roots:
            row = self.roots[row["invocation_id"]]
        row["status"] = status
        for key, value in kwargs.items():
            if value is not None:
                row[{"provider_id": "provider_response_id"}.get(key, key)] = value
        return row.copy()

    def attempts(self, run_id, stage):
        return [
            row.copy() for row in self.roots.values() if row["run_id"] == run_id and row["stage"] == stage
        ]

    def prepare(self, run_id, stage, body, context, **kwargs):
        row = dict(
            self.root,
            invocation_id=UUID(body["metadata"]["podgen_invocation_id"]),
            status="prepared",
            input_tokens=None,
            output_tokens=None,
            provider_response_id=None,
            output_record_id=None,
            request_record_id=self.store_request(body),
        )
        row.update({key: value for key, value in kwargs.items() if key != "authorization"})
        self.records[row["request_record_id"]].update(
            context=deepcopy(context), authorization=deepcopy(kwargs["authorization"])
        )
        self.roots[row["invocation_id"]] = row
        return row.copy()


def install_memory_turns(monkeypatch):
    class MemoryTurnJournal(model_turns.TurnJournal):
        def prepare(self, body):
            journal = self.journal
            row = next(
                (
                    row
                    for row in journal.turns
                    if row["root_invocation_id"] == self.root["invocation_id"]
                    and row["turn_index"] == self.index
                ),
                None,
            )
            if row is not None:
                assert journal.request(row) == body
                return row.copy()
            row = dict(
                self.root,
                invocation_id=uuid5(self.root["invocation_id"], f"source-turn:{self.index}"),
                turn_index=self.index,
                root_invocation_id=self.root["invocation_id"],
                status="prepared",
                attempt_index=0,
                input_tokens=None,
                output_tokens=None,
                provider_response_id=None,
                request_record_id=journal.store_request(body),
            )
            journal.records[row["request_record_id"]]["context"] = deepcopy(
                journal.records[self.root["request_record_id"]]["context"]
            )
            journal.turns.append(row)
            return row.copy()

        def transition(self, invocation, status, **kwargs):
            return self.journal.transition(
                next(
                    row for row in self.journal.turns if row["invocation_id"] == invocation["invocation_id"]
                ),
                status,
                **kwargs,
            )

    def aggregate(journal, root, value, status):
        journal.roots[root["invocation_id"]].update(
            status=status,
            input_tokens=value["input"],
            output_tokens=None if value["unknown"] else value["known_output"],
        )

    monkeypatch.setattr(model_turns, "TurnJournal", MemoryTurnJournal)
    monkeypatch.setattr(
        model_turns,
        "rows",
        lambda journal, root: deepcopy(
            [row for row in journal.turns if row["root_invocation_id"] == root["invocation_id"]]
        ),
    )
    monkeypatch.setattr(model_turns, "aggregate", aggregate)
