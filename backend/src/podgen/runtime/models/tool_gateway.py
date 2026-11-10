"""Durable tool orchestration around the ordinary counted, guarded provider submission."""

import json
from copy import deepcopy
from uuid import uuid5

from jsonschema import Draft202012Validator, ValidationError

from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.validation import load_unique_json

from .contracts import WireReply


def _account(journal, root, profile, history):
    value = model_turns.usage(journal, root, profile.output_limit, history=history)
    model_turns.aggregate(journal, root, value, "tool_running")
    return value


def _calls(response):
    output = response.get("output")
    if not isinstance(output, list):
        raise ContractError("provider_output_missing")
    calls = []
    for item in output:
        if not isinstance(item, dict):
            raise ContractError("provider_malformed_output")
        if item.get("type") == "function_call":
            if not all(
                isinstance(item.get(key), str) and item[key] for key in ("call_id", "name", "arguments")
            ):
                raise ContractError("provider_malformed_tool_call")
            if item.get("status") not in (None, "completed"):
                raise ContractError("provider_tool_call_incomplete")
            calls.append(item)
        elif item.get("type") == "message":
            if item.get("role") != "assistant" or item.get("status") not in (None, "completed"):
                raise ContractError("provider_malformed_tool_message")
            contents = item.get("content")
            if not isinstance(contents, list) or any(not isinstance(content, dict) for content in contents):
                raise ContractError("provider_malformed_content")
            if any(content.get("type") == "refusal" for content in contents):
                raise ContractError("provider_refused")
        elif item.get("type") not in ("message", "reasoning"):
            raise ContractError("unexpected_provider_tool_or_output")
    if len({call["call_id"] for call in calls}) != len(calls):
        raise ContractError("duplicate_provider_tool_call_id")
    return calls


def _execute(journal, root, corpus, contract, call, index, turn, guard, history):
    saved = [
        record
        for record in history.of_kind("source_tool_result")
        if record["data"].get("call_index") == index
    ]
    if saved:
        data = saved[0]["data"]
        if (
            data["call_id"] != call["call_id"]
            or data["name"] != call["name"]
            or data["raw_arguments"] != call["arguments"]
        ):
            raise Conflict("source_tool_call_changed")
        return data["result"]
    try:
        arguments = load_unique_json(call["arguments"])
    except ValueError:
        arguments = None
    data = dict(
        turn_index=turn,
        call_index=index,
        read_id=f"E{index + 1}",
        call_id=call["call_id"],
        name=call["name"],
        raw_arguments=call["arguments"],
        arguments=arguments,
    )
    intents = [
        record for record in history.of_kind("source_tool_call") if record["data"].get("call_index") == index
    ]
    if not intents:
        ref = journal.event(root, "source_tool_call", data=data)
        history.appended(ref, "source_tool_call")
    elif intents[0]["data"] != data:
        raise Conflict("source_tool_call_changed")
    definition = next((tool for tool in contract["definitions"] if tool["name"] == call["name"]), None)
    if definition is None:
        result = dict(error="unknown_source_tool")
    else:
        try:
            Draft202012Validator(definition["parameters"]).validate(arguments)
        except ValidationError:
            result = dict(error="invalid_source_tool_arguments")
        else:
            guard()
            result = corpus.execute(
                call["name"],
                arguments,
                read_id=data["read_id"],
                max_read_chars=contract.get("max_read_chars", 2400),
            )
    if not isinstance(result, dict):
        raise ContractError("invalid_source_tool_result")
    if "passage" in result and (
        "error" in result
        or call["name"] != "read_source"
        or result["passage"].get("read_id") != data["read_id"]
    ):
        raise ContractError("source_tool_read_binding_mismatch")
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    ref = journal.event(root, "source_tool_result", data=dict(data, result=result))
    history.appended(ref, "source_tool_result")
    return result


def invoke_tools(gateway, root, profile, contract, *, limits, totals, guard):
    journal = gateway.journal
    if root["status"] in ("failed", "validation_failed", "outcome_unknown"):
        gateway._fail(root, "invocation_requires_new_attempt_or_reconciliation", status=root["status"])
    if gateway.source_corpus is None:
        raise ContractError("source_tool_corpus_missing")
    original = journal.request(root)
    if original.get("tools") != contract["definitions"] or original.get("tool_choice") != "auto":
        raise Conflict("source_tool_request_contract_changed")
    conversation = deepcopy(original["input"])
    history = model_turns.EventHistory(journal, root)
    call_ids = set()
    call_index = 0
    for index in range(contract["max_model_turns"]):
        guard()
        body = deepcopy(original)
        body["input"] = deepcopy(conversation)
        body["metadata"]["podgen_invocation_id"] = str(uuid5(root["invocation_id"], f"source-turn:{index}"))
        body["metadata"]["podgen_source_turn"] = str(index)
        final_turn = (
            index == contract["max_model_turns"] - 1
            and contract.get("final_turn") == "structured_without_tools"
        )
        if final_turn:
            body["tool_choice"] = "none"
        turn_journal = model_turns.TurnJournal(journal, root, index, history=history)
        turn = turn_journal.prepare(body)
        # Current turn is not included twice when resuming an already submitted step.
        previous = [row for row in model_turns.rows(journal, root) if row["turn_index"] < index]
        prior_input = sum(row["input_tokens"] or 0 for row in previous)
        prior_output = sum(
            row["output_tokens"] if row["output_tokens"] is not None else profile.output_limit
            for row in previous
        )
        physical = type(gateway)(
            turn_journal, gateway.provider_factory, sleep=gateway.sleep, poll_seconds=gateway.poll_seconds
        )
        try:
            response = physical.invoke_once(
                turn,
                profile,
                limits=limits,
                totals=dict(input=totals["input"] + prior_input, output=totals["output"] + prior_output),
                guard=guard,
                full_response=True,
            )
        except RunFailure:
            _account(journal, root, profile, history)
            current = model_turns.rows(journal, root)[-1]
            journal.transition(root, current["status"])
            raise
        usage = _account(journal, root, profile, history)
        calls = _calls(response)
        if final_turn and calls:
            gateway._fail(root, "tool_call_after_tools_closed", status="validation_failed")
        if not calls:
            text = gateway._output_text(root, response)
            final = deepcopy(response)
            final["metadata"] = original["metadata"]
            final["usage"] = dict(
                input_tokens=usage["input"], output_tokens=None if usage["unknown"] else usage["known_output"]
            )
            existing = history.of_kind("source_tools_final_response")
            if not existing:
                journal.event(
                    root,
                    "source_tools_final_response",
                    reply=WireReply(json.dumps(final, ensure_ascii=False, allow_nan=False).encode()),
                    data=dict(
                        provider_response_id=final["id"], turn_index=index, turn_id=str(turn["invocation_id"])
                    ),
                )
            model_turns.aggregate(journal, root, usage, "response_completed")
            journal.transition(root, "response_completed", provider_id=final["id"])
            return text
        if call_index + len(calls) > contract["max_tool_calls"]:
            gateway._fail(root, "source_tool_call_limit_exceeded", status="validation_failed")
        if profile.provider == "anthropic":
            if not isinstance(response.get("anthropic_content"), list):
                raise ContractError("anthropic_tool_continuation_missing")
            conversation.append(
                dict(type="anthropic_assistant", content=deepcopy(response["anthropic_content"]))
            )
        else:
            conversation.extend(deepcopy(response["output"]))
        for call in calls:
            if call["call_id"] in call_ids:
                raise ContractError("duplicate_provider_tool_call_id")
            call_ids.add(call["call_id"])
            result = _execute(
                journal, root, gateway.source_corpus, contract, call, call_index, index, guard, history
            )
            conversation.append(
                dict(
                    type="function_call_output",
                    call_id=call["call_id"],
                    output=json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                    **({"is_error": True} if profile.provider == "anthropic" and "error" in result else {}),
                )
            )
            call_index += 1
    gateway._fail(root, "source_tool_turn_limit_exceeded", status="validation_failed")
