"""Explicit model-authored checkpoints, exact native counts and replayable context segments."""

import json
from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.compiler import count_request
from podgen.runtime.evaluation.contracts import RunFailure

from .contracts import ProviderFault


class ResearchContext:
    def __init__(self, gateway, root, profile, original, history, guard):
        self.gateway, self.root, self.profile = gateway, root, profile
        self.original, self.history, self.guard = original, history, guard
        self.journal = gateway.journal

    def event(self, kind, data, *, reply=None):
        ref = self.journal.event(self.root, kind, data=data, reply=reply)
        self.history.appended(ref, kind)
        return ref

    def measured(self, body, index, label):
        key = digest(body)
        for record in self.history.of_kind("source_context_count"):
            if record["data"]["request_hash"] == key:
                return record["data"]["input_tokens"]
        self.guard()
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
            if type(count) is not int or count < 0:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ContractError("invalid_context_input_count") from None
        self.event(
            "source_context_count",
            dict(request_hash=key, input_tokens=count, turn_index=index, label=label),
            reply=reply,
        )
        return count

    def request(self, body, index, segment_start):
        saved = next(
            (
                row
                for row in self.history.of_kind("source_context_plan")
                if row["data"]["turn_index"] == index
            ),
            None,
        )
        if saved:
            if saved["data"]["conversation_hash"] != digest(body):
                raise Conflict("research_context_plan_changed")
            checkpoint = saved["data"]["checkpoint"]
        else:
            count = self.measured(body, index, "normal")
            capacity = self.profile.context_limit - self.profile.output_limit
            # Reserve a complete checkpoint response and one maximum UTF-8 tool
            # page before the hard model window. This is a soft checkpoint trigger,
            # not a separate admission ceiling on a normal task.
            checkpoint = index > segment_start and count + self.profile.output_limit + 4 * 9600 >= capacity
            self.event(
                "source_context_plan",
                dict(turn_index=index, conversation_hash=digest(body), checkpoint=checkpoint),
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
            self.event(
                "source_context_unfinished",
                dict(turn_index=index, reason="checkpoint_does_not_fit_model_window"),
            )
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
        existing = next(
            (
                row
                for row in self.history.of_kind("source_context_segment")
                if row["data"]["next_turn"] == turn_index + 1
            ),
            None,
        )
        if existing:
            return existing
        blob = self.journal.blob(
            json.dumps(conversation, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
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
