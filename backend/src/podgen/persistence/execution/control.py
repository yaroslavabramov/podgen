"""The closed set of journal control links. Content references do not enter here."""

import hashlib
from uuid import UUID, uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.database import one

from .control_bundle import OUTPUTS, load


class Controls:
    def __init__(self, tx, project, run_id, set_id, generation, records, metadata=None):
        self.tx, self.project, self.run_id = tx, project, run_id
        self.set_id, self.generation = set_id, generation
        self.metadata = metadata
        self.pending = {ref.record_id: (ref, payload) for ref, payload, _ in records}
        self.allowed = {}
        self.edges = []
        self.borrowed = {}
        self.parents, self.invocations, self.turns = {}, {}, {}
        self.parents, self.invocations, self.turns, self.calls = load(
            tx, project, run_id, set_id, generation, records, self.pending, metadata
        )

    def error(self):
        raise ContractError("execution_control_binding_mismatch")

    def parent(self, value):
        try:
            ref = RuntimeRef.model_validate(value)
        except (ValueError, TypeError):
            self.error()
        if ref.record_id in self.pending:
            expected, payload = self.pending[ref.record_id]
            if ref != expected:
                self.error()
            return ref, payload
        row = self.parents.get(ref.record_id)
        if row is None or row["record_kind"] != ref.record_kind:
            self.error()
        return ref, row["payload"]

    def add(self, consumer, path, ref):
        self.allowed[consumer.record_id, path] = ref
        self.edges.append((consumer.record_id, ref))

    def request(self, consumer, payload):
        try:
            invocation = UUID(payload["invocation_id"])
        except (ValueError, KeyError, TypeError):
            self.error()
        actual = self.invocations.get(invocation)
        ref, parent = self.parent(payload.get("request_ref"))
        if parent.get("contract") != "model-request-v1" or parent.get("invocation_id") != str(invocation):
            self.error()
        if actual is None or actual["request_record_id"] != ref.record_id:
            self.error()
        self.add(consumer, "/request_ref", ref)
        return invocation

    def turn(self, consumer, payload, invocation):
        data = payload.get("data", {})
        if "turn_request_ref" not in data:
            return
        ref, parent = self.parent(data["turn_request_ref"])
        actual = self.turns.get((invocation, data.get("source_turn_index")))
        if actual is not None:
            if actual["request_record_id"] != ref.record_id or str(actual["invocation_id"]) != data.get(
                "source_turn_id"
            ):
                self.error()
        else:
            turn = parent.get("context", {}).get("source_tool_turn", {})
            try:
                physical = UUID(data["source_turn_id"])
            except (ValueError, TypeError, KeyError):
                self.error()
            if physical != uuid5(
                invocation, f"source-turn:{data['source_turn_index']}"
            ) or ref.record_id != uuid5(physical, "request"):
                self.error()
            if (
                ref.record_id not in self.pending
                or parent.get("contract") != "model-request-v1"
                or parent.get("invocation_id") != data.get("source_turn_id")
                or turn.get("root_invocation_id") != str(invocation)
                or turn.get("turn_index") != data.get("source_turn_index")
            ):
                self.error()
        self.add(consumer, "/data/turn_request_ref", ref)

    def event_parents(self, consumer, payload):
        data = payload.get("data", {})
        kind = payload.get("event_kind")
        if "count_intent_ref" in data:
            ref, parent = self.parent(data["count_intent_ref"])
            if (
                kind not in ("source_context_count", "source_context_count_error")
                or parent.get("event_kind") != "source_context_count_intent"
                or parent.get("invocation_id") != payload.get("invocation_id")
            ):
                self.error()
            self.add(consumer, "/data/count_intent_ref", ref)
            native = data.get("native_request_blob")
            if native is not None:
                if parent.get("data", {}).get("native_request_blob") != native:
                    self.error()
                self.borrowed[consumer.record_id, "/data/native_request_blob"] = ref
        if "count_ref" in data:
            ref, parent = self.parent(data["count_ref"])
            if (
                kind != "count_receipt_reused"
                or parent.get("event_kind") != "source_context_count"
                or data.get("input_tokens") != parent.get("data", {}).get("input_tokens")
            ):
                self.error()
            previous_identity = parent.get("data", {}).get("count_identity")
            if previous_identity is not None and previous_identity != data.get("count_identity"):
                self.error()
            if previous_identity is None:
                proof = one(
                    self.tx,
                    """SELECT 1 FROM operational_records WHERE project_id=:p
                    AND record_kind='legacy_exact_count' AND record_id=:id
                    AND payload->>'count_identity'=:identity AND (payload->>'input_tokens')::bigint=:tokens""",
                    p=self.project,
                    id=ref.record_id,
                    identity=data.get("count_identity"),
                    tokens=data.get("input_tokens"),
                )
                if proof is None:
                    self.error()
            self.add(consumer, "/data/count_ref", ref)
            target = data.get("turn_request_ref")
            if target:
                request, _ = self.parent(target)
                if request.record_id in self.pending:
                    self.edges.append((request.record_id, ref))
        if "checkpoint_result_ref" in data:
            ref, parent = self.parent(data["checkpoint_result_ref"])
            prior = parent.get("data", {})
            if (
                kind != "source_context_segment"
                or parent.get("event_kind") != "source_tool_result"
                or parent.get("invocation_id") != payload.get("invocation_id")
                or prior.get("name") != "save_working_state"
                or prior.get("call_index") != data.get("checkpoint_call_index")
            ):
                self.error()
            self.add(consumer, "/data/checkpoint_result_ref", ref)
        if "provider_response_ref" in data:
            ref, parent = self.parent(data["provider_response_ref"])
            prior = parent.get("data", {})
            if (
                parent.get("event_kind") != "provider_response"
                or parent.get("invocation_id") != payload.get("invocation_id")
                or prior.get("source_turn_index") != data.get("turn_index")
            ):
                self.error()
            self.add(consumer, "/data/provider_response_ref", ref)
            if kind in ("source_tool_call", "source_tool_result"):
                call = self.calls.get((ref.record_id, data.get("call_id")))
                raw = data.get("raw_arguments")
                if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != data.get(
                    "arguments_hash"
                ):
                    self.error()
                if (
                    call is None
                    or call["arguments_hash"] != data.get("arguments_hash")
                    or call["tool_name"] != data.get("name")
                ):
                    self.error()

    def collect(self, records):
        for ref, payload, _ in records:
            contract = payload.get("contract")
            if contract == "model-invocation-event-v1" or contract in OUTPUTS:
                invocation = self.request(ref, payload)
                if contract == "model-invocation-event-v1":
                    self.turn(ref, payload, invocation)
                    self.event_parents(ref, payload)
            if contract == "model-request-v1" and payload.get("parent_request_ref") is not None:
                parent, body = self.parent(payload["parent_request_ref"])
                if body.get("contract") != "model-request-v1":
                    self.error()
                self.add(ref, "/parent_request_ref", parent)
        return self

    def is_bound_control(self, consumer, path, ref):
        if self.allowed.get((consumer.record_id, path)) != ref:
            return False
        return (
            ref.record_id in self.pending or self.parents[ref.record_id]["execution_input_set_id"] is not None
        )

    def blob_owner(self, record_id, path, ref):
        parent = self.borrowed.get((record_id, path))
        if parent is None:
            return False
        key = ("blob_binding", self.project, self.run_id, parent.record_id, ref)
        if self.metadata is not None and self.metadata.get(key) is not None:
            return True
        row = one(
            self.tx,
            """SELECT 1 FROM runtime_blob_refs
            WHERE project_id=:p AND record_id=:parent AND storage_key=:key
             AND sha256=:sha AND byte_length=:length AND media_type=:media""",
            p=self.project,
            parent=parent.record_id,
            key=ref.storage_key,
            sha=ref.sha256,
            length=ref.byte_length,
            media=ref.media_type,
        )
        if row is not None and self.metadata is not None:
            self.metadata.put(key, True)
        return row is not None
