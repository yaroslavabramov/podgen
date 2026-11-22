"""The normal next-turn candidate, before external input measurement."""

from copy import deepcopy
from uuid import uuid5

from podgen.runtime.evaluation.research_cache_layout import mark_turn
from podgen.runtime.evaluation.source_tool_contract import role_definitions


def turn_request(original, conversation, root, index):
    body = deepcopy(dict(original, input=conversation))
    body["metadata"].update(
        podgen_invocation_id=str(uuid5(root["invocation_id"], f"source-turn:{index}")),
        podgen_source_turn=str(index),
    )
    return body


def normal_request(original, conversation, root, index, contract, provider):
    body = turn_request(original, conversation, root, index)
    body["tools"] = role_definitions(contract, root["stage"], provider)
    body["tool_choice"] = "auto"
    mark_turn(body)
    return body
