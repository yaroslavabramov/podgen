"""Only required, located edits reach the author; complete reviews remain independent artifacts."""

from copy import deepcopy

from podgen.domain.errors import ContractError


def required(finding):
    return finding["priority"] in ("blocking", "important")


def required_readings(readings):
    """A review without a required task cannot independently grant a larger edit scope."""
    if not any(required(finding) for review in readings.values() for finding in review["findings"]):
        raise ContractError("revision_required_findings_missing")
    result = deepcopy(readings)
    for review in result.values():
        if not any(required(finding) for finding in review["findings"]):
            review["revision_scope"] = dict(scale="none")
    return result


def tasks(readings, support):
    cards = {card.get("card_id", card.get("label")): card for card in support}
    result = []
    for origin in ("reader", "fact"):
        for finding in readings[origin]["findings"]:
            if not required(finding):
                continue
            task = dict(
                origin=origin,
                anchors=deepcopy(finding["anchors"]),
                problem=finding["observation"],
                action=finding["requested_change"],
            )
            if finding["preserve"]:
                task["preserve"] = deepcopy(finding["preserve"])
            if origin == "fact":
                evidence, context = [], []
                for identity in dict.fromkeys(finding["support_labels"]):
                    if identity not in cards:
                        raise ContractError("revision_support_card_missing", identity)
                    excerpts = cards[identity].get("source_excerpts", [])
                    if excerpts:
                        evidence.append(
                            dict(card_id=identity, source_excerpt_indices=list(range(len(excerpts))))
                        )
                    else:
                        context.append(dict(card_id=identity))
                if evidence:
                    task["evidence_refs"] = evidence
                if context:
                    task["support_refs"] = context
            result.append(task)
    if not result:
        raise ContractError("revision_required_findings_missing")
    return result
