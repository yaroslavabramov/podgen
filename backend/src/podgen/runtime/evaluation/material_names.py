"""Human references resolve from the run's frozen packet, never current workspace names."""

import json
from collections import Counter
from uuid import uuid5

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import ContractError


def frozen_names(journal, run_id, manifest):
    if manifest.get("input_contract") != "editorial-role-packets/1":
        return None
    ref = RuntimeRef(record_kind="context_assembly", record_id=uuid5(run_id, "manifest"))
    frozen = json.loads(journal.read_blob(ref, BlobRef.model_validate(manifest["materials_blob"])))
    packet = frozen["editorial_input"]
    if packet["contract"] != "editorial-input/2":
        return None
    return {card["card_id"]: card["label"] for card in packet["dossier"]["knowledge_cards"]}


def display_materials(references, names):
    if names is None:
        return ", ".join(references)
    counts = Counter(names.values())

    def display(value):
        if value not in names:
            raise ContractError("unknown_editorial_report_card", value)
        name = names[value]
        return name + " [" + value + "]" if counts[name] > 1 else name

    return ", ".join(display(value) for value in references)
