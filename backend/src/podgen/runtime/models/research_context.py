"""Explicit model-authored checkpoints, exact native counts and replayable context segments."""

import json
from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.compiler import count_request
from podgen.runtime.evaluation.contracts import RunFailure

from .contracts import ProviderFault
from .count_receipts import CONTRACT, ExactCount, identity, native_count, provider_configuration


class ResearchContext:
    def __init__(self, gateway, root, profile, original, history, guard, *, history_view=None):
        self.gateway, self.root, self.profile = gateway, root, profile
        self.original, self.history, self.guard = original, history, guard
        self.journal = gateway.journal
        self.history_view = history_view
        self.pending_events = []

    def event(self, kind, data, *, reply=None):
        phase = self.publication()
        ref = phase.event(kind, data=data, reply=reply, history=self.history)
        phase.commit()
        return ref

    def publication(self):
        phase = self.journal.publication(self.root)
        for kind, data in self.pending_events:
            phase.event(kind, data=data, history=self.history)
        self.pending_events = []
        return phase

    def measured(self, body, index, label):
        key = digest(body)
        count_identity = identity(body, self.profile)
        saved = self.receipt(body)
        if saved is not None:
            return saved.input_tokens
        phase = self.publication()
        native_blob = phase.blob(
            json.dumps(
                native_count(body, self.profile.provider),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        )
        intent_data = dict(
            count_identity=count_identity,
            count_contract=CONTRACT,
            native_request_blob=native_blob.model_dump(mode="json"),
            provider_configuration=provider_configuration(self.profile),
            turn_index=index,
            label=label,
        )
        admission_refs = getattr(self.guard, "admission_refs", lambda: [])()
        if admission_refs:
            intent_data["runtime_admission_refs"] = [ref.model_dump(mode="json") for ref in admission_refs]
        intent = phase.event("source_context_count_intent", data=intent_data, history=self.history)
        phase.commit(guard=self.guard)
        try:
            reply = self.gateway.provider_factory(self.profile).count(count_request(body))
        except ProviderFault as error:
            self.event(
                "source_context_count_error", dict(request_hash=key, code=error.code), reply=error.reply
            )
            raise RunFailure(
                error.code, run_id=self.root["run_id"], invocation_id=self.root["invocation_id"]
            ) from None
        try:
            count = json.loads(reply.body)["input_tokens"]
            if type(count) is not int or not 0 <= count <= 2**63 - 1:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            self.event(
                "source_context_count_error",
                dict(
                    request_hash=key,
                    code="invalid_context_input_count",
                    count_intent_ref=intent.model_dump(mode="json"),
                ),
                reply=reply,
            )
            raise ContractError("invalid_context_input_count") from None
        self.event(
            "source_context_count",
            dict(
                request_hash=key,
                input_tokens=count,
                turn_index=index,
                label=label,
                count_identity=count_identity,
                count_contract=CONTRACT,
                count_intent_ref=intent.model_dump(mode="json"),
                native_request_blob=native_blob.model_dump(mode="json"),
                provider_configuration=provider_configuration(self.profile),
            ),
            reply=reply,
        )
        return count

    def receipt(self, body):
        saved = self.history.exact_count(identity(body, self.profile))
        if saved:
            return ExactCount(saved.reference, saved.data["count_identity"], saved.data["input_tokens"])
        from .legacy_count_receipts import receipt

        with self.journal.read_operation():
            return receipt(self.journal, self.history.counted(digest(body)), body, self.profile)

    def request(self, body, index, segment_start):
        self.pending_events = []
        saved = self.history.context_plan(index)
        if saved:
            if saved.data["conversation_hash"] != digest(body):
                raise Conflict("research_context_plan_changed")
            checkpoint = saved.data["checkpoint"]
        else:
            count = self.measured(body, index, "normal")
            capacity = self.profile.context_limit - self.profile.output_limit
            # Reserve a complete checkpoint response and one maximum UTF-8 tool
            # page before the hard model window. This is a soft checkpoint trigger,
            # not a separate admission ceiling on a normal task.
            checkpoint = index > segment_start and count + self.profile.output_limit + 4 * 9600 >= capacity
            self.pending_events.append(
                (
                    "source_context_plan",
                    dict(turn_index=index, conversation_hash=digest(body), checkpoint=checkpoint),
                )
            )
        result = deepcopy(body)
        if checkpoint:
            result["tools"] = [tool for tool in body["tools"] if tool["name"] == "save_working_state"]
            result["input"].append(
                dict(
                    role="user",
                    content=[
                        dict(
                            type="input_text",
                            text=json.dumps(
                                dict(
                                    runtime_checkpoint="Before this model window fills, save complete working notes and a separate compact resume_state using save_working_state. Do not issue an editorial verdict. All full messages and receipts remain available through work history."
                                ),
                                ensure_ascii=False,
                            ),
                        )
                    ],
                )
            )
        if (
            self.measured(result, index, "checkpoint" if checkpoint else "normal") + self.profile.output_limit
            > self.profile.context_limit
        ):
            phase = self.journal.publication(self.root)
            for kind, data in self.pending_events:
                phase.event(kind, data=data, history=self.history)
            phase.event(
                "source_context_unfinished",
                data=dict(turn_index=index, reason="checkpoint_does_not_fit_model_window"),
                history=self.history,
            )
            phase.commit()
            self.pending_events = []
            raise RunFailure(
                "research_context_checkpoint_unavailable",
                run_id=self.root["run_id"],
                invocation_id=self.root["invocation_id"],
            )
        return result, checkpoint

    def publish(self, conversation, call_index, turn_index):
        saved = self.history.for_call("source_tool_result", call_index)[0]
        if saved["data"]["name"] != "save_working_state" or "error" in saved["data"]["result"]:
            raise ContractError("research_checkpoint_not_saved")
        existing = self.history.context_segment(turn_index + 1)
        if existing:
            return self.history.read(existing.reference)
        blob = self.journal.blob(
            json.dumps(conversation, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        )
        view = {}
        if self.history_view is not None:
            from . import research_history_view

            if self.history_view != research_history_view.VERSION:
                raise ContractError("unsupported_research_history_view")
            semantic = research_history_view.encoded(research_history_view.conversation(conversation))
            view = dict(
                history_view=self.history_view,
                history_blob=self.journal.blob(semantic.encode()).model_dump(mode="json"),
            )
        ref = self.event(
            "source_context_segment",
            dict(
                next_turn=turn_index + 1,
                checkpoint_call_index=call_index,
                checkpoint_result_ref=RuntimeRef(
                    record_kind="tool_execution", record_id=saved["_record_id"]
                ).model_dump(mode="json"),
                conversation_blob=blob.model_dump(mode="json"),
                **view,
            ),
        )
        return self.history.read(ref)

    def restart(self, segment):
        index = segment["data"]["checkpoint_call_index"]
        saved = self.history.for_call("source_tool_result", index)[0]
        args = saved["data"]["arguments"]
        payload = dict(
            runtime_continuation=dict(
                checkpoint=saved["data"]["result"]["checkpoint"],
                resume_state=args["resume_state"],
                receipt_ids=args["receipt_ids"],
                full_working_state=dict(tool="read_work_history", kind="tool_result", index=index),
                archived_conversations=dict(tool="list_work_history", offset=0),
            )
        )
        return deepcopy(self.original["input"]) + [
            dict(
                role="user",
                content=[
                    dict(
                        type="input_text", text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    )
                ],
            )
        ]
