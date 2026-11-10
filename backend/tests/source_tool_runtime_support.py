"""In-memory persistence boundary for real gateway and provider protocol tests."""

from copy import deepcopy
from uuid import uuid4, uuid5

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
            input_tokens=None,
            output_tokens=None,
            provider_response_id=None,
            output_record_id=None,
        )
        self.root["request_record_id"] = self.store_request(body)

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
        return self.receipts.copy()

    def event(self, root, kind, *, reply=None, data=None):
        ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        record = dict(event_kind=kind, data=deepcopy(data or {}))
        if reply:
            record["response_blob"] = self.blob(reply.body).model_dump(mode="json")
        self.records[ref.record_id] = record
        self.receipts.append(dict(record_id=ref.record_id, event_kind=kind))
        return ref

    def transition(self, row, status, **kwargs):
        if row["invocation_id"] == self.root["invocation_id"]:
            row = self.root
        row["status"] = status
        for key, value in kwargs.items():
            if value is not None:
                row[{"provider_id": "provider_response_id"}.get(key, key)] = value
        return row.copy()


def install_memory_turns(monkeypatch):
    class MemoryTurnJournal(model_turns.TurnJournal):
        def prepare(self, body):
            journal = self.journal
            if len(journal.turns) > self.index:
                row = journal.turns[self.index]
                assert journal.request(row) == body
                return row.copy()
            row = dict(
                self.root,
                invocation_id=uuid5(self.root["invocation_id"], f"source-turn:{self.index}"),
                turn_index=self.index,
                status="prepared",
                attempt_index=0,
                input_tokens=None,
                output_tokens=None,
                provider_response_id=None,
                request_record_id=journal.store_request(body),
            )
            journal.turns.append(row)
            return row.copy()

        def transition(self, row, status, **kwargs):
            return self.journal.transition(self.journal.turns[self.index], status, **kwargs)

    def aggregate(journal, root, value, status):
        journal.root.update(
            status=status,
            input_tokens=value["input"],
            output_tokens=None if value["unknown"] else value["known_output"],
        )

    monkeypatch.setattr(model_turns, "TurnJournal", MemoryTurnJournal)
    monkeypatch.setattr(model_turns, "rows", lambda journal, root: deepcopy(journal.turns))
    monkeypatch.setattr(model_turns, "aggregate", aggregate)
