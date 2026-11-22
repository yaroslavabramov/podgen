"""Exact edit windows and conservative, occurrence-safe anchor alignment."""

from collections import defaultdict
from copy import deepcopy
from difflib import SequenceMatcher

from podgen.domain.errors import ContractError


def comparison(before, after):
    def keys(script):
        return [(row["speaker"], row["text"]) for row in script["utterances"]]

    old, new = keys(before), keys(after)
    windows = []
    for kind, first, last, start, end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if kind == "equal":
            continue
        windows.append(
            dict(
                kind=kind,
                before=[dict(index=i, **deepcopy(before["utterances"][i])) for i in range(first, last)],
                after=[dict(index=i, **deepcopy(after["utterances"][i])) for i in range(start, end)],
            )
        )
    old_positions, new_positions = defaultdict(list), defaultdict(list)
    for i, key in enumerate(old):
        old_positions[key].append(i)
    for i, key in enumerate(new):
        new_positions[key].append(i)
    aligned = {
        positions[0]: new_positions[key][0]
        for key, positions in old_positions.items()
        if len(positions) == 1 and len(new_positions[key]) == 1
    }
    return windows, aligned


def align_anchors(anchors, before, after, alignment):
    current = []
    missing = False
    for anchor in anchors:
        index = anchor["utterance_index"]
        if (
            not 0 <= index < len(before["utterances"])
            or anchor["quote"] not in before["utterances"][index]["text"]
        ):
            raise ContractError("audit_previous_anchor_invalid")
        if index not in alignment:
            missing = True
            continue
        target = alignment[index]
        if anchor["quote"] not in after["utterances"][target]["text"]:
            raise ContractError("audit_recheck_alignment_invalid")
        current.append(dict(anchor, utterance_index=target))
    return None if missing else current


def attach(view, before, after):
    changes, alignment = comparison(before, after)
    view.pop("previous_script_in_order", None)
    view["script_changes"] = changes
    for row in view["previous_decisions"]:
        anchors = align_anchors(row["anchors"], before, after, alignment)
        row["anchor_version"] = "previous" if anchors is None else "current"
        if anchors is not None:
            previous_indices = [item["utterance_index"] for item in row["anchors"]]
            if previous_indices != [item["utterance_index"] for item in anchors]:
                row["previous_utterance_indices"] = previous_indices
            row["anchors"] = anchors
        row["requires_update"] = anchors is None
        if anchors is None:
            positions = [alignment.get(item["utterance_index"]) for item in row["anchors"]]
            if any(position is not None for position in positions):
                row["current_utterance_indices"] = positions
    for task in view["assignments"]:
        task["anchor_version"] = "previous"
        anchors = align_anchors(task["anchors"], before, after, alignment)
        if anchors is not None:
            task["current_utterance_indices"] = [item["utterance_index"] for item in anchors]
        else:
            indices = {anchor["utterance_index"] for anchor in task["anchors"]}
            task["change_indices"] = [
                i
                for i, change in enumerate(changes)
                if indices.intersection(row["index"] for row in change["before"])
            ]
    return view
