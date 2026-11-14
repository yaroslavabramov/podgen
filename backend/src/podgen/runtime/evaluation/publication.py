"""A bounded private checkpoint builder. Byte staging has no database side effects."""

import hashlib
import json
from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.private_journal.head import next_head
from podgen.persistence.private_journal.objects import prepare
from podgen.persistence.private_journal.writer import commit


class Publication:
    def __init__(self, journal, invocation):
        self.owner, self.invocation = journal, invocation
        self.journal = getattr(journal, "journal", journal)
        self.root = getattr(journal, "root", invocation)
        self.run_id = self.root["run_id"]
        self.previous = self.journal.execution.current_head(self.run_id)
        if self.previous is None and self.journal.run(self.run_id)["execution_storage"] == "legacy":
            from .legacy_adoption import adopt

            self.previous = adopt(self.journal, self.root, invocation)
        self.views, self.blobs, self.committed = [], {}, []
        self.private_uses = []
        self.event_callbacks, self.legacy_counts = {}, []
        self.state, self.physical, self.request_snapshot = None, None, None
        self.finished = False

    def absorb(self, phase, *, prepend=False):
        if phase.finished or phase.run_id != self.run_id:
            raise RuntimeError("invalid_publication_merge")
        self.views = phase.views + self.views if prepend else self.views + phase.views
        self.blobs.update(phase.blobs)
        self.private_uses.extend(phase.private_uses)
        self.committed.extend(phase.committed)
        self.event_callbacks.update(phase.event_callbacks)
        self.legacy_counts.extend(phase.legacy_counts)
        if phase.physical is not None:
            self.physical = phase.physical
            self.request_snapshot = phase.request_snapshot
        phase.finished = True

    def blob(self, data, media="application/json"):
        key = data, media
        if key not in self.blobs:
            self.blobs[key] = prepare(data, self.journal.project, media)
        return self.blobs[key].reference

    def reuse_blob(self, reference, owner_reference):
        self.private_uses.append((reference, owner_reference))
        return reference

    def record(
        self, payload, *, record_id=None, kind="tool_execution", replay=False, projection="identity/3"
    ):
        revision = (self.previous["revision"] if self.previous else 0) + 1
        slot = hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        identity = record_id or uuid5(self.invocation["invocation_id"], f"archive/3/{revision}/{slot}")
        ref = RuntimeRef(record_kind=kind, record_id=identity)
        self.views.append(
            dict(
                record_id=str(identity),
                record_kind=kind,
                projection=projection,
                payload=dict(payload, journal_version="execution-journal/3"),
            )
        )
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
            for field, raw in (
                ("response_blob", reply.body),
                ("wire_response_blob", getattr(reply, "wire_response", None)),
                ("wire_request_blob", getattr(reply, "request_body", None)),
            ):
                if raw is not None:
                    payload[field] = self.blob(raw).model_dump(mode="json")
            payload.update(
                provider_http_status=reply.status_code,
                provider_request_id=reply.request_id,
                secret_redacted=reply.redacted,
            )
        ref = self.record(payload)
        saved = self.views[-1]["payload"]
        if kind == "provider_response" and reply is not None:
            from podgen.persistence.execution.provider_calls import project_calls

            self.provider_calls = project_calls(self.journal.project, self.run_id, ref, saved, reply.body)
        provider = (data or {}).get("provider_response_id")
        if provider:
            self.transition(self.state[0] if self.state else self.invocation["status"], provider_id=provider)
        history = history if history is not None else getattr(self.owner, "history", None)
        if history is not None:
            self.event_callbacks[ref.record_id] = lambda: history.appended(
                ref, kind, data=value, record=saved
            )
        return ref

    def transition(self, status, **fields):
        self.state = (
            status,
            dict(self.state[1] if self.state else {}, **{k: v for k, v in fields.items() if v is not None}),
        )

    def capture_count_after_dispatch_failure(self, input_tokens):
        if self.finished:
            return
        removed = {
            view["record_id"] for view in self.views if view["payload"].get("event_kind") == "submit_intent"
        }
        self.views = [view for view in self.views if view["record_id"] not in removed]
        self.event_callbacks = {
            key: callback for key, callback in self.event_callbacks.items() if str(key) not in removed
        }
        if self.views:
            self.state = "counted", dict(input_tokens=input_tokens)
            return self.commit()

    def commit(self, *, guard=None):
        if self.finished:
            raise RuntimeError("publication_already_committed")
        if guard is not None:
            prepare_guard = getattr(guard, "prepare_publication", None)
            (prepare_guard if prepare_guard is not None else guard)()
        from .checkpoint_compiler import compile_checkpoint

        command = compile_checkpoint(self, action=guard is not None)
        try:
            result = commit(self.journal.repo, self.journal.project, command)
        except Conflict:
            self.journal.execution.invalidate()
            raise
        self.finished = True
        self.entry_id = command.entry_id
        if not result["created"] and "intent" in command.kind:
            self.journal.execution.invalidate()
            from .contracts import RunFailure

            raise RunFailure(
                "submission_acknowledgement_unknown",
                run_id=self.run_id,
                invocation_id=self.invocation["invocation_id"],
            )
        self.journal.execution.committed(self.run_id, next_head(self.previous, command))
        self.journal.execution.charges_committed(command)
        self.journal.execution.remember_checkpoint(command)
        row = dict(self.invocation)
        from podgen.persistence.private_journal.state import identity_row

        row.update(identity_row(command.data.get("state", {})))
        if self.physical is not None:
            physical = dict(self.physical)
            physical.update(command.data.get("state", {}))
            self.journal.execution.remember_physical(
                self.run_id, self.root["invocation_id"], physical, self.request_snapshot
            )
        elif self.invocation.get("turn_index") is not None:
            cached = self.journal.execution.current_physical(
                self.run_id, self.root["invocation_id"], self.invocation["turn_index"]
            )
            if cached:
                self.journal.execution.remember_physical(
                    self.run_id, self.root["invocation_id"], row, cached[1]
                )
        for operation in (*self.committed, *self.event_callbacks.values()):
            operation()
        ledger = getattr(self.owner, "ledger", None)
        if ledger is not None:
            ledger.committed(row)
        self.blobs.clear()
        if command.data.get("accounting_error"):
            from .contracts import RunFailure

            raise RunFailure(
                "run_token_usage_overflow", run_id=self.run_id, invocation_id=self.invocation["invocation_id"]
            )
        return row
