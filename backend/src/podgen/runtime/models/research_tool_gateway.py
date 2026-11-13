"""Unbounded semantic research task over durable counted turns and explicit model windows."""

import json
from copy import deepcopy
from itertools import count
from uuid import uuid5

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.research_cache_layout import mark_turn

from .contracts import WireReply
from .research_context import ResearchContext
from .research_continuation import append_response, resolve_structured, structured_requests
from .research_history import ResearchHistory
from .research_tools import ResearchTools
from .tool_gateway import _account, _calls, _execute


def invoke(gateway, root, profile, contract, *, limits, totals, guard):
    journal = gateway.journal
    if root["status"] in {"failed", "validation_failed", "outcome_unknown"}:
        gateway._fail(root, "invocation_requires_new_attempt_or_reconciliation", status=root["status"])
    corpus = gateway.source_corpus
    if corpus is None:
        raise ContractError("source_tool_corpus_missing")
    from podgen.runtime.evaluation.source_tool_recovery import continuation, lineage, turn_input_policy

    start_turn, prefix_receipts = continuation(journal, root)
    input_policy = turn_input_policy(journal, root)
    ancestors = lineage(journal, root)
    original = journal.request(root)
    task = journal.request(ancestors[0])
    history = ResearchHistory(journal, root)
    final = history.latest("source_tools_final_response")
    if final:
        from podgen.domain.contracts.common import BlobRef, RuntimeRef

        ref = RuntimeRef(record_kind="tool_execution", record_id=final["_record_id"])
        response = json.loads(journal.read_blob(ref, BlobRef.model_validate(final["response_blob"])))
        usage = _account(journal, root, profile, history)
        model_turns.aggregate(journal, root, usage, "response_completed")
        journal.transition(root, "response_completed", provider_id=response["id"])
        return gateway._output_text(root, response)
    proxy = ResearchTools(journal, root, corpus, history, guard, history_view=contract.get("history_view"))
    if corpus.material_resolver is not None:
        proxy.restore(corpus.material_resolver.restore(run_id=root["run_id"], guard=guard))
        corpus.refresh()
    context = ResearchContext(
        gateway, root, profile, task, history, guard, history_view=contract.get("history_view")
    )
    segment = history.latest("source_context_segment")
    segment_start = segment["data"]["next_turn"] if segment else 0
    call_index = segment["data"]["checkpoint_call_index"] + 1 if segment else 0
    if prefix_receipts and not (
        segment
        and segment["invocation_id"] == str(root["invocation_id"])
        and segment["data"]["next_turn"] > start_turn
    ):
        segment_start = start_turn
        call_index = len(prefix_receipts)
        conversation = deepcopy(original["input"])
    else:
        conversation = context.restart(segment) if segment else deepcopy(original["input"])
    for index in count(segment_start):
        guard()
        body = deepcopy(original)
        body["input"] = deepcopy(conversation)
        body["metadata"].update(
            podgen_invocation_id=str(uuid5(root["invocation_id"], f"source-turn:{index}")),
            podgen_source_turn=str(index),
        )
        retry_first = bool(root.get("attempt_index", 0) and index == start_turn)
        if retry_first:
            # Authorization binds this exact unsent/refused physical request.
            # Do not reinterpret its already selected context/checkpoint mode.
            checkpoint_requested = [tool["name"] for tool in body["tools"]] == ["save_working_state"]
        else:
            body["tools"] = deepcopy(contract["definitions"])
            body["tool_choice"] = "auto"
            mark_turn(body)
            body, checkpoint_requested = context.request(body, index, segment_start)
        turn_journal = model_turns.TurnJournal(journal, root, index, history=history)
        turn = turn_journal.prepare(body)
        previous = [row for row in model_turns.rows(journal, root) if row["turn_index"] < index]
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
                    input=totals["input"] + sum(row["input_tokens"] or 0 for row in previous),
                    output=totals["output"]
                    + sum(
                        row["output_tokens"] if row["output_tokens"] is not None else profile.output_limit
                        for row in previous
                    ),
                ),
                guard=guard,
                full_response=True,
                input_limits=input_policy.get(index, input_policy[None]),
            )
        except RunFailure as error:
            _account(journal, root, profile, history)
            current = model_turns.rows(journal, root)[-1]
            journal.transition(root, current["status"])
            raise RunFailure(error.code, run_id=root["run_id"], invocation_id=root["invocation_id"]) from None
        usage = _account(journal, root, profile, history)
        calls = _calls(response)
        if not calls:
            if checkpoint_requested:
                context.event(
                    "source_context_unfinished", dict(turn_index=index, reason="checkpoint_not_returned")
                )
                raise RunFailure(
                    "research_context_checkpoint_missing",
                    run_id=root["run_id"],
                    invocation_id=root["invocation_id"],
                )
            requests = structured_requests(response, index, history)
            if requests:
                call_index = resolve_structured(
                    requests,
                    gateway,
                    root,
                    proxy,
                    contract,
                    history,
                    conversation,
                    response,
                    profile,
                    call_index,
                    index,
                    guard,
                )
                continue
            text = gateway._output_text(root, response)
            final = deepcopy(response)
            final["metadata"] = original["metadata"]
            final["usage"] = dict(
                input_tokens=usage["input"], output_tokens=None if usage["unknown"] else usage["known_output"]
            )
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
        checkpoints = [call for call in calls if call["name"] == "save_working_state"]
        if checkpoints and len(calls) != 1 or checkpoint_requested and not checkpoints:
            raise ContractError("research_checkpoint_requires_one_state_call")
        if checkpoint_requested:
            conversation = deepcopy(body["input"])
        append_response(conversation, response, profile.provider)
        for call in calls:
            existing = history.for_call("source_tool_call", call_index)
            if call["call_id"] in history.call_ids and not existing:
                raise ContractError("duplicate_provider_tool_call_id")
            result = _execute(journal, root, proxy, contract, call, call_index, index, guard, history)
            conversation.append(
                dict(
                    type="function_call_output",
                    call_id=call["call_id"],
                    output=json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                    **({"is_error": True} if profile.provider == "anthropic" and "error" in result else {}),
                )
            )
            call_index += 1
        if checkpoints:
            if "error" in result:
                raise RunFailure(
                    "research_context_checkpoint_invalid",
                    run_id=root["run_id"],
                    invocation_id=root["invocation_id"],
                )
            segment = context.publish(conversation, call_index - 1, index)
            conversation = context.restart(segment)
            segment_start = index + 1
