"""In-memory persistence boundary for real gateway and provider protocol tests."""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.publication import Publication


class MemoryJournal:
    def __init__(self, manifest, body):
        self.snapshot = manifest
        self.project = uuid4()
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
        self.execution = SimpleNamespace(invocation=lambda _: None, prepare=lambda *args, **kwargs: None)

    def runtime_graph(self):
        return nullcontext()

    def read_operation(self):
        return nullcontext()

    def publication(self, invocation):
        return MemoryPublication(self, invocation)

    def event_binding(self, invocation, data):
        return invocation, dict(data or {})

    def blob_many(self, values):
        return [self.blob(data, media) for data, media in values]

    def blob(self, data, media="application/json"):
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

    def text_blob(self, ref, blob):
        return self.read_blob(ref, blob).decode()

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

    def events_for_turn(self, root, turn_index):
        return [event for event in self.events(root) if event.get("source_turn_index") == turn_index]

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
            dict(
                record_id=ref.record_id,
                event_kind=kind,
                invocation_id=root["invocation_id"],
                source_turn_index=(data or {}).get("source_turn_index"),
            )
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


class MemoryPublication(Publication):
    """The same phase contract over the protocol tests' in-memory journal."""

    def commit(self, *, guard=None):
        if guard is not None:
            guard()
        journal = self.journal
        for ref, payload, _replay in self.records:
            journal.records[ref.record_id] = deepcopy(payload)
        for event in self.events:
            journal.receipts.append(
                dict(
                    record_id=event["record_id"],
                    event_kind=event["event_kind"],
                    invocation_id=event["invocation_id"],
                    source_turn_index=event["source_turn_index"],
                )
            )
        result = self.invocation
        if self.state is not None:
            result = self.owner.transition(self.invocation, self.state[0], **self.state[1])
        self.finished = True
        for callback in (*self.committed, *self.event_callbacks.values()):
            callback()
        ledger = getattr(self.owner, "ledger", None)
        if ledger is not None:
            ledger.committed(result)
            if ledger.history.is_submitted(result["turn_index"]):
                usage = ledger.usage()
                journal.roots[self.owner.root["invocation_id"]].update(
                    status=result["status"]
                    if result["status"] in ("failed", "validation_failed", "outcome_unknown")
                    else "tool_running",
                    input_tokens=usage["input"],
                    output_tokens=None if usage["unknown"] else usage["known_output"],
                )
        return result


def install_memory_turns(monkeypatch):
    monkeypatch.setattr("podgen.runtime.evaluation.provider_evidence.register_structured", lambda *args: None)

    class MemoryTurnJournal(model_turns.TurnJournal):
        def publication(self, invocation):
            return MemoryPublication(self, invocation)

        def stage_prepare(self, body, *, pending_events=(), admission_refs=(), original_record=None):
            turn = self.prepare(body)
            phase = self.publication(turn)
            for kind, data in pending_events:
                pending = self.journal.publication(self.root)
                pending.event(kind, data=data, history=self.history)
                phase.absorb(pending)
            return (
                turn,
                phase,
                (
                    body,
                    self.journal.read(
                        RuntimeRef(record_kind="tool_execution", record_id=turn["request_record_id"])
                    ),
                ),
            )

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
