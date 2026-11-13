"""Explicit publication phase: staged bytes, atomic evidence/state, committed history."""

from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.runtime_publication import append_evaluation_batch


class Publication:
    def __init__(self, journal, invocation):
        self.owner, self.invocation = journal, invocation
        self.journal = getattr(journal, "journal", journal)
        self.records, self.events, self.writes, self.committed = [], [], [], []
        self.blobs = {}
        self.state = None
        self.finished = False

    def blob(self, data, media="application/json"):
        key = (data, media)
        if key not in self.blobs:
            self.blobs[key] = self.journal.blob(data, media)
        return self.blobs[key]

    def record(self, payload, *, record_id=None, kind="tool_execution", replay=False):
        ref = RuntimeRef(record_kind=kind, record_id=record_id or uuid4())
        self.records.append((ref, payload, replay))
        return ref

    def event(self, kind, *, reply=None, data=None, history=None):
        root, value = self.owner.event_binding(self.invocation, data)
        payload = dict(
            contract="model-invocation-event-v1",
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
            self.committed.append(lambda: history.appended(ref, kind, data=value, record=payload))
        return ref

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
            if self.records:
                append_evaluation_batch(self.journal.repo, tx, self.journal.project, self.records)
            else:
                self.journal.authorize(tx)
            # Classification can protect a previously public SHA alias. The fresh
            # action authorization must see those own writes before dispatch is
            # committed; a denial rolls the whole phase back, with no provider I/O.
            if transactional_guard is not None:
                transactional_guard(tx)
            insert_rows(tx, "evaluation_invocation_events", self.events)
            for operation in self.writes:
                operation(tx)
            if self.state is not None:
                return self.owner.write_transition(tx, self.invocation, self.state[0], **self.state[1])
            return self.invocation

        result = archive_write(self.journal.repo, self.journal.project, write)
        self.finished = True
        for operation in self.committed:
            operation()
        ledger = getattr(self.owner, "ledger", None)
        if ledger is not None:
            ledger.committed(result)
        self.blobs.clear()
        return result
