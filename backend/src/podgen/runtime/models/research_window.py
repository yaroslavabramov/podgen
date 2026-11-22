"""Admission for a normal research turn and its state-saving successor."""

import json
from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import Conflict

from .web_search import context_limit

FACT_KEY = "fact_context_policy"
MEASURED = "measured-fact-window/1"


def needs_checkpoint(body, profile, measured_input, policy):
    window = context_limit(body, profile)
    output = profile.output_limit
    if policy == MEASURED:
        # A working answer must fit its actual tools' window. The following
        # state-saving call has only a local tool, so uses the model's window.
        # Incoming tool results are counted when they exist.
        return measured_input > min(window - output, profile.context_limit - 2 * output)
    return measured_input + 2 * output + 4 * 9600 >= window


def checkpoint_request(body, *, prior_input_items=None, instructions=None):
    from podgen.writing.prompts.research_runtime import legacy

    instructions = instructions if instructions is not None else legacy()
    inputs = body["input"]
    instruction = instructions["checkpoint"]
    if prior_input_items is not None:
        inputs = inputs[:prior_input_items]
        instruction += instructions["deferred"]
    result = deepcopy(
        dict(body, input=inputs, tools=[t for t in body["tools"] if t.get("name") == "save_working_state"])
    )
    result["input"].append(
        dict(
            role="user",
            content=[dict(type="input_text", text=json.dumps(dict(runtime_checkpoint=instruction)))],
        )
    )
    return result


def preserve_deferred(context, index, request, tail):
    """The frozen paid request omits this exchange, including on an authorized retry."""
    binding = digest(request["input"])
    current = context.history.latest("source_context_deferred_exchange")
    if current and current["data"]["turn_index"] == index:
        if current["data"]["checkpoint_input_hash"] != binding:
            raise Conflict("deferred_checkpoint_changed")
        return
    phase = context.publication()
    blob = phase.blob(json.dumps(tail, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    phase.event(
        "source_context_deferred_exchange",
        data=dict(
            turn_index=index, checkpoint_input_hash=binding, exchange_blob=blob.model_dump(mode="json")
        ),
        history=context.history,
    )
    phase.commit()


def retry_checkpoint_input(context, index, request):
    inputs = request["input"]
    current = context.history.latest("source_context_deferred_exchange")
    if current is None or current["data"]["turn_index"] != index:
        return list(inputs)
    data = current["data"]
    if data["checkpoint_input_hash"] != digest(inputs):
        raise Conflict("deferred_checkpoint_changed")
    ref = context.history.references["source_context_deferred_exchange"][-1]
    tail = json.loads(context.journal.read_blob(ref, BlobRef.model_validate(data["exchange_blob"])))
    return [*inputs[:-1], *tail, inputs[-1]]
