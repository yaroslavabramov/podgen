"""Durable tool orchestration around the ordinary counted, guarded provider submission."""

import hashlib
import json
from copy import deepcopy
from uuid import uuid5

from jsonschema import Draft202012Validator, ValidationError

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.validation import load_unique_json

from .contracts import WireReply


def _account(journal, root, profile, history, *, ledger=None):
    value = (
        ledger.usage()
        if ledger is not None
        else model_turns.usage(journal, root, profile.output_limit, history=history)
    )
    if value["input"] > 2**63 - 1 or value["known_output"] > 2**63 - 1:
        phase = journal.publication(root)
        phase.event("failure", data=dict(code="run_token_usage_overflow", status="validation_failed"))
        phase.transition("validation_failed")
        phase.commit()
        raise RunFailure(
            "run_token_usage_overflow", run_id=root["run_id"], invocation_id=root["invocation_id"]
        )
    if ledger is None or ledger.needs_reconciliation:
        model_turns.aggregate(journal, root, value, "tool_running")
        if ledger is not None:
            ledger.needs_reconciliation = False
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
            from podgen.persistence.execution.provider_calls import valid_identifier

            if not valid_identifier(item["call_id"], max_bytes=512) or not valid_identifier(
                item["name"], max_bytes=256
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
    saved = history.for_call("source_tool_result", index)
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
    provider_events = history.of_kind("provider_response")
    provider_event = next(
        (row for row in reversed(provider_events) if row["data"].get("source_turn_index") == turn), None
    )
    if provider_event is None:
        raise ContractError("source_tool_provider_response_missing")
    from podgen.runtime.evaluation.provider_evidence import adopt_response

    adopt_response(journal, root, provider_event)
    data.update(
        provider_response_ref=RuntimeRef(
            record_kind="tool_execution", record_id=provider_event["_record_id"]
        ).model_dump(mode="json"),
        arguments_hash=hashlib.sha256(call["arguments"].encode()).hexdigest(),
    )
    intents = history.for_call("source_tool_call", index)
    durable_intent = call["name"] in ("request_source", "request_research", "save_working_state")
    if not intents and durable_intent:
        phase = journal.publication(root)
        phase.event("source_tool_call", data=data, history=history)
        phase.commit(guard=guard)
    elif intents:
        saved_data = intents[0]["data"]
        expected = (
            data
            if intents[0].get("journal_version")
            else {
                key: value
                for key, value in data.items()
                if key not in ("provider_response_ref", "arguments_hash")
            }
        )
        if saved_data != expected:
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
            if intents or not durable_intent:
                guard()
            from podgen.retrieval import RetrievalUnavailable

            try:
                result = corpus.execute(
                    call["name"],
                    arguments,
                    read_id=data["read_id"],
                    max_read_chars=contract.get("max_read_chars", 2400),
                    **(
                        {"default_read_chars": contract["default_read_chars"]}
                        if "default_read_chars" in contract
                        else {}
                    ),
                )
            except RetrievalUnavailable as error:
                # Keep the physical response and unfinished tool intent resumable; never re-submit it.
                journal.event(root, "source_search_unavailable", data=dict(call_index=index, code=str(error)))
                raise RunFailure(
                    str(error), run_id=root["run_id"], invocation_id=root["invocation_id"]
                ) from None

    if (
        isinstance(result, dict)
        and result.get("status") == "pending"
        and call["name"] in ("request_source", "request_research")
    ):
        journal.event(root, "source_material_pending", data=dict(data, result=result))
        raise RunFailure(
            "source_material_handoff_pending", run_id=root["run_id"], invocation_id=root["invocation_id"]
        )
    if not isinstance(result, dict):
        raise ContractError("invalid_source_tool_result")
    if "passage" in result and (
        "error" in result
        or call["name"] != "read_source"
        or result["passage"].get("read_id") != data["read_id"]
    ):
        raise ContractError("source_tool_read_binding_mismatch")
    if "material_passage" in result and (
        "error" in result
        or call["name"] not in ("read_material", "read_research_result")
        or result["material_passage"].get("read_id") != "R" + data["read_id"].removeprefix("E")
    ):
        raise ContractError("material_tool_read_binding_mismatch")
    diagnostics = getattr(corpus, "last_search_diagnostics", None)
    phase = journal.publication(root)
    if call["name"] == "search_sources" and diagnostics is not None:
        phase.event("source_search_diagnostics", data=dict(call_index=index, **diagnostics))
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    if not durable_intent and not intents:
        phase.event("source_tool_call", data=data, history=history)
    phase.event("source_tool_result", data=dict(data, result=result), history=history)
    phase.commit()
    return result


def invoke_tools(gateway, root, profile, contract, *, limits, totals, guard):
    if contract["version"] == "frozen-corpus-tools/7":
        from .research_tool_gateway import invoke

        return invoke(gateway, root, profile, contract, limits=limits, totals=totals, guard=guard)
    journal = gateway.journal
    if root["status"] in ("failed", "validation_failed", "outcome_unknown"):
        gateway._fail(root, "invocation_requires_new_attempt_or_reconciliation", status=root["status"])
    if gateway.source_corpus is None:
        raise ContractError("source_tool_corpus_missing")
    from podgen.runtime.evaluation.source_tool_recovery import continuation, turn_input_policy

    start_turn, prefix_receipts = continuation(journal, root)
    input_policy = turn_input_policy(journal, root)
    original = journal.request(root)
    original_record = journal.read(
        RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"])
    )
    manifest_snapshot = journal.manifest(root["run_id"])
    allowed_choices = {"auto"}
    if (
        start_turn == contract["max_model_turns"] - 1
        and contract.get("final_turn") == "structured_without_tools"
    ):
        allowed_choices.add("none")
    if original.get("tools") != contract["definitions"] or original.get("tool_choice") not in allowed_choices:
        raise Conflict("source_tool_request_contract_changed")
    conversation = deepcopy(original["input"])
    history = model_turns.EventHistory(journal, root)
    from podgen.runtime.evaluation.turn_ledger import TurnLedger

    ledger = TurnLedger(journal, root, profile.output_limit, history)
    call_ids = {record["data"]["call_id"] for record in prefix_receipts}
    call_index = len(prefix_receipts)
    for index in range(start_turn, contract["max_model_turns"]):
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
        from podgen.runtime.evaluation.research_cache_layout import mark_turn

        mark_turn(body)
        turn_journal = model_turns.TurnJournal(journal, root, index, history=history, ledger=ledger)
        turn, prepared_phase, request_snapshot = turn_journal.stage_prepare(
            body,
            original_record=original_record,
            admission_refs=getattr(guard, "admission_refs", lambda: [])(),
        )
        # Current turn is not included twice when resuming an already submitted step.
        prior = ledger.before(index)
        physical = type(gateway)(
            turn_journal,
            gateway.provider_factory,
            sleep=gateway.sleep,
            poll_seconds=gateway.poll_seconds,
            total_input_ceiling=gateway.total_input_ceiling,
        )
        try:
            response = physical.invoke_once(
                turn,
                profile,
                limits=limits,
                totals=dict(
                    input=totals["input"] + prior["input"], output=totals["output"] + prior["output"]
                ),
                guard=guard,
                full_response=True,
                prepared_phase=prepared_phase,
                request_snapshot=request_snapshot,
                manifest_snapshot=manifest_snapshot,
                input_limits=input_policy.get(index, input_policy[None]),
            )
        except RunFailure as error:
            _account(journal, root, profile, history, ledger=ledger)
            current = model_turns.rows(journal, root)[-1]
            journal.transition(root, current["status"])
            raise RunFailure(error.code, run_id=root["run_id"], invocation_id=root["invocation_id"]) from None
        usage = _account(journal, root, profile, history, ledger=ledger)
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
            phase = journal.publication(root)
            if not existing:
                phase.event(
                    "source_tools_final_response",
                    reply=WireReply(json.dumps(final, ensure_ascii=False, allow_nan=False).encode()),
                    data=dict(
                        provider_response_id=final["id"], turn_index=index, turn_id=str(turn["invocation_id"])
                    ),
                )
            phase.transition(
                "response_completed",
                provider_id=final["id"],
                input_tokens=usage["input"],
                output_tokens=None if usage["unknown"] else usage["known_output"],
            )
            phase.commit()
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
