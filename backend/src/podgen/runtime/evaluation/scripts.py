"""Technical script versions preserve full output and explicit, conservative local identity."""

from collections import Counter
from uuid import uuid5


def script_view(invocation_id, output, previous=None):
    old = previous.get("spoken_view", []) if previous else []
    old_counts = Counter((item["speaker"], item["text"]) for item in old)
    new_counts = Counter((item["speaker"], item["text"]) for item in output["utterances"])
    exact = {
        (item["speaker"], item["text"]): item["utterance_id"]
        for item in old
        if old_counts[(item["speaker"], item["text"])] == 1
    }
    items = []
    for index, item in enumerate(output["utterances"]):
        key = (item["speaker"], item["text"])
        retained = key in exact and new_counts[key] == 1
        items.append(
            dict(
                utterance_id=exact[key] if retained else str(uuid5(invocation_id, f"utterance:{index}")),
                index=index,
                speaker=item["speaker"],
                text=item["text"],
                identity_basis="unique_exact_match" if retained else "new_unmapped",
            )
        )
    return items
