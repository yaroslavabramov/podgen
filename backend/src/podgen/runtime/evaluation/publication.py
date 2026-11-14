"""Explicit publication phase: staged bytes, atomic evidence/state, committed history."""

from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.batch_insert import insert_rows


class Publication:
    def __init__(self, journal, invocation):
        self.owner, self.invocation = journal, invocation
        self.journal = getattr(journal, "journal", journal)
        self.records, self.events, self.writes, self.committed = [], [], [], []
        self.blobs = {}
        self.event_callbacks = {}
        self.provider_calls = []
        root = getattr(journal, "root", invocation)
        self.run_id = root["run_id"]
        self.journal.execution.invocation(root)
        if invocation["request_record_id"] != root["request_record_id"]:
            self.journal.execution.invocation(invocation)
        self.proofs = []
        self.state = None
        self.finished = False

    def absorb(self, phase, *, prepend=False):
        if phase.finished or phase.run_id != self.run_id:
            raise RuntimeError("invalid_publication_merge")
        self.records = phase.records + self.records if prepend else self.records + phase.records
        self.events = phase.events + self.events if prepend else self.events + phase.events
        self.writes.extend(phase.writes)
        self.proofs.extend(phase.proofs)
        self.committed.extend(phase.committed)
        self.event_callbacks.update(phase.event_callbacks)
        self.provider_calls.extend(phase.provider_calls)
        self.blobs.update(phase.blobs)
        phase.finished = True

    def blob(self, data, media="application/json"):
        key = (data, media)
        if key not in self.blobs:
            self.blobs[key] = self.journal.blob(data, media)
        return self.blobs[key]

    def record(self, payload, *, record_id=None, kind="tool_execution", replay=False):
        ref = RuntimeRef(record_kind=kind, record_id=record_id or uuid4())
        payload = dict(payload, journal_version="execution-journal/2")
        self.records.append((ref, payload, replay))
        return ref

    def event(self, kind, *, reply=None, data=None, history=None):
        root, value = self.owner.event_binding(self.invocation, data)
        payload = dict(
            contract="model-invocation-event-v1",
            journal_version="execution-journal/2",
            invocation_id=str(root["invocation_id"]),
            event_kind=kind,
            request_ref=RuntimeRef(
                record_kind="tool_execution", record_id=root["request_record_id"]
            ).model_dump(mode="json"),
            data=value,
        )
        if reply is not None:
            raw_fields = {"response_blob": reply.body}
            for attribute, field in (
                ("wire_response", "wire_response_blob"),
                ("request_body", "wire_request_blob"),
            ):
                raw = getattr(reply, attribute, None)
                if raw is not None:
                    raw_fields[field] = raw
            missing = list(
                dict.fromkeys(
                    (raw, "application/json")
                    for raw in raw_fields.values()
                    if (raw, "application/json") not in self.blobs
                )
            )
            if missing:
                saved = self.journal.blob_many(missing)
                self.blobs.update(zip(missing, saved, strict=True))
            payload.update(
                {
                    field: self.blobs[raw, "application/json"].model_dump(mode="json")
                    for field, raw in raw_fields.items()
                }
            )
            payload.update(
                provider_http_status=reply.status_code,
                provider_request_id=reply.request_id,
                secret_redacted=reply.redacted,
            )
        ref = self.record(payload)
        if kind == "provider_response" and reply is not None:
            from podgen.persistence.execution.provider_calls import project_calls

            self.provider_calls.extend(
                project_calls(self.journal.project, self.run_id, ref, payload, reply.body)
            )
        self.events.append(
            dict(
                project_id=self.journal.project,
                invocation_id=root["invocation_id"],
                event_id=uuid4(),
                record_id=ref.record_id,
                event_kind=kind,
                source_turn_index=value.get("source_turn_index"),
            )
        )
        provider = (data or {}).get("provider_response_id")
        if provider:
            self.transition(self.state[0] if self.state else self.invocation["status"], provider_id=provider)
        history = history if history is not None else getattr(self.owner, "history", None)
        if history is not None:

            def committed_event():
                history.appended(ref, kind, data=value, record=payload)

            self.event_callbacks[ref.record_id] = committed_event
        return ref

    def capture_count_after_dispatch_failure(self, input_tokens):
        """Keep received evidence while removing an uncommitted dispatch intent."""
        if self.finished:
            return
        submit_ids = {event["record_id"] for event in self.events if event["event_kind"] == "submit_intent"}
        self.records = [record for record in self.records if record[0].record_id not in submit_ids]
        self.events = [event for event in self.events if event["record_id"] not in submit_ids]
        for record_id in submit_ids:
            self.event_callbacks.pop(record_id, None)
        if self.records:
            self.state = ("counted", dict(input_tokens=input_tokens))
            return self.commit()

    def transition(self, status, **fields):
        previous = self.state[1] if self.state else {}
        self.state = (status, dict(previous, **{k: v for k, v in fields.items() if v is not None}))

    def commit(self, *, guard=None):
        if self.finished:
            raise RuntimeError("publication_already_committed")

        transactional_guard = (
            getattr(guard, "authorize_publication", None)
            if getattr(guard, "supports_publication_snapshot", True)
            else None
        )
        if guard is not None and transactional_guard is None:
            guard()

        def write(tx):
            for proof in self.proofs:
                proof(tx)
            if self.records:
                self.journal.execution.append(
                    tx, self.run_id, self.records, self.journal.execution.owned(self.blobs)
                )
            else:
                self.journal.authorize(tx)
            # Classification can protect a previously public SHA alias. The fresh
            # action authorization must see those own writes before dispatch is
            # committed; a denial rolls the whole phase back, with no provider I/O.
            if transactional_guard is not None:
                transactional_guard(tx)
            insert_rows(tx, "evaluation_invocation_events", self.events)
            insert_rows(tx, "execution_provider_calls", self.provider_calls)
            for operation in self.writes:
                operation(tx)
            if self.state is not None:
                return self.owner.write_transition(tx, self.invocation, self.state[0], **self.state[1])
            return self.invocation

        result = self.journal.execution.publish(write)
        self.finished = True
        for operation in (*self.committed, *self.event_callbacks.values()):
            operation()
        ledger = getattr(self.owner, "ledger", None)
        if ledger is not None:
            ledger.committed(result)
        self.blobs.clear()
        return result
