"""Route explicit structured research requests back into the same unfinished role."""

import json
from copy import deepcopy

from podgen.domain.canonical import digest

from .tool_gateway import _execute


def append_response(conversation, response, provider):
    if provider == "anthropic":
        conversation.append(dict(type="anthropic_assistant", content=deepcopy(response["anthropic_content"])))
    else:
        conversation.extend(deepcopy(response["output"]))


def structured_requests(response, turn, history):
    messages = [
        part["text"]
        for item in response["output"]
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    ]
    try:
        value = json.loads("".join(messages))
    except (ValueError, TypeError):
        return []
    if not isinstance(value, dict):
        return []
    handled = {
        row["data"]["request_hash"]
        for row in history.of_kind("source_structured_material_request")
        if row["data"]["turn_index"] != turn
    }
    return [
        request
        for request in value.get("research_requests", [])
        if isinstance(request, dict)
        and request.get("blocking", True) is not False
        and isinstance(request.get("question"), str)
        and digest(request) not in handled
    ]


def structured_call(request, turn, position, *, anchored=False):
    arguments = dict(
        question=request["question"],
        reason=request.get("why_needed") or request.get("reason") or json.dumps(request, ensure_ascii=False),
    )
    if anchored:
        arguments["anchors"] = request.get("anchors", [])
    return dict(
        type="function_call",
        call_id=f"structured_research_{turn}_{position}",
        name="request_research",
        arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )


def resolve_structured(
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
    turn,
    guard,
):
    calls = [
        structured_call(request, turn, position, anchored=proxy.anchored)
        for position, request in enumerate(requests)
    ]
    event = history.provider_response(turn)
    from podgen.runtime.evaluation.provider_evidence import register_structured

    decision_entry = register_structured(gateway.journal, root, event, requests, calls, turn, history)
    if decision_entry is not None:
        calls = [dict(call, decision_entry_id=str(decision_entry)) for call in calls]
    results = []
    for request, call in zip(requests, calls, strict=True):
        result = _execute(gateway.journal, root, proxy, contract, call, call_index, turn, guard, history)
        results.append(dict(request=request, result=result))
        if not any(
            row["data"].get("turn_index") == turn and row["data"].get("request_hash") == digest(request)
            for row in history.of_kind("source_structured_material_request")
        ):
            ref = gateway.journal.event(
                root,
                "source_structured_material_request",
                data=dict(
                    turn_index=turn, request_hash=digest(request), request=request, call_index=call_index
                ),
            )
            history.appended(
                ref,
                "source_structured_material_request",
                data=dict(turn_index=turn, request_hash=digest(request)),
            )
        call_index += 1
    append_response(conversation, response, profile.provider)
    conversation.append(
        dict(
            role="user",
            content=[
                dict(
                    type="input_text",
                    text=json.dumps(
                        dict(
                            material_request_resolutions=results,
                            runtime_continuation="Continue this same assessment. Inspect the returned material through the reading tools, preserve unresolved limitations, and finish the existing role output when its necessary work is complete. Retrieval is not verification.",
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            ],
        )
    )
    return call_index
