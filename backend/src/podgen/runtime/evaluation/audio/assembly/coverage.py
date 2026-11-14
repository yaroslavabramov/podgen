"""Coalesce adjacent exact character spans before comparing full script coverage."""

from podgen.domain.errors import ContractError


def coalesce(selectors, utterances):
    if len(selectors) != len(utterances):
        raise ContractError("assembly_selector_utterance_mismatch")
    selected, spoken = [], []
    for selector, utterance in zip(selectors, utterances, strict=True):
        if selected and selector["utterance_id"] == selected[-1]["utterance_id"]:
            previous = selected[-1]
            if (
                selector["utterance_index"] != previous["utterance_index"]
                or selector["char_start"] != previous["char_end"]
                or {k: v for k, v in utterance.items() if k != "text"}
                != {k: v for k, v in spoken[-1].items() if k != "text"}
            ):
                raise ContractError("assembly_noncontiguous_utterance_parts")
            previous["char_end"] = selector["char_end"]
            spoken[-1]["text"] += utterance["text"]
        else:
            selected.append(dict(selector))
            spoken.append(dict(utterance))
    return selected, spoken
