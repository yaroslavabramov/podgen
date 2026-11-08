"""Constrain the writer's speaker field to the labels in this episode's assignment."""

from copy import deepcopy


def bind_script_speakers(schema: dict, speaker_labels: tuple[str, ...]) -> dict:
    utterance = schema.get("$defs", {}).get("WriteUtterance")
    if not speaker_labels or utterance is None:
        return schema
    bound = deepcopy(schema)
    bound["$defs"]["WriteUtterance"]["properties"]["speaker"]["enum"] = list(speaker_labels)
    return bound
