"""Explicit creation/migration only. Reading and import never generate or repair card identities."""

from copy import deepcopy
from uuid import uuid4

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from .card_catalog import catalog
from .editorial_contract import EditorialInput, FixedEditorialInput, KnowledgeCard


def _remap_support(dossier, keys):
    groups = [dossier["summary_support_labels"], *(thread["support_labels"] for thread in dossier["threads"])]
    for refs in groups:
        refs[:] = [keys[ref] for ref in refs]


def new_card_id(existing):
    while True:
        value = "C" + uuid4().hex[:12]
        if value not in existing:
            return value


def upgrade_shared(data):
    packet = EditorialInput.model_validate(data)
    if packet.contract != "editorial-input/1":
        raise ContractError("editorial_upgrade_requires_v1")
    source = packet.model_dump(mode="json")
    result = deepcopy(source)
    result["contract"] = "editorial-input/2"
    mapping = {}
    for card in result["dossier"]["knowledge_cards"]:
        previous = card["label"]
        mapping[previous] = card["card_id"] = new_card_id(set(mapping.values()))
    _remap_support(result["dossier"], mapping)
    result = EditorialInput.model_validate(result).model_dump(mode="json")
    return result, dict(
        contract="editorial-card-migration/1",
        source_digest=digest(source),
        target_digest=digest(result),
        card_ids=mapping,
    )


def upgrade_fixed(data, shared):
    fixed = FixedEditorialInput.model_validate(data)
    packet = EditorialInput.model_validate(shared)
    if fixed.contract != "fixed-editorial-input/1" or packet.contract != "editorial-input/2":
        raise ContractError("editorial_fixed_migration_binding_mismatch")

    # Reconstruct the previous packet from this exact target. A standalone lookup table
    # could swap two valid IDs while still naming the correct source and target digests.
    previous = packet.model_dump(mode="json")
    previous["contract"] = "editorial-input/1"
    dossier = previous["dossier"]
    labels = {card.pop("card_id"): card["label"] for card in dossier["knowledge_cards"]}
    _remap_support(dossier, labels)
    mapping = {label: key for key, label in labels.items()}
    if (
        len(mapping) != len(labels)
        or fixed.shared_input_digest != digest(previous)
        or fixed.assignment_key != packet.assignment.key
    ):
        raise ContractError("editorial_fixed_migration_binding_mismatch")
    result = fixed.model_dump(mode="json")
    body = result["plan"]
    groups = [request["material_refs"] for request in body["research_requests"]]
    if body["plan"]:
        groups += [
            body["plan"]["opening"]["material_refs"],
            *(act["material_refs"] for act in body["plan"]["acts"]),
        ]
    for group in groups:
        if set(group) - set(mapping):
            raise ContractError("editorial_unknown_migration_card")
        group[:] = [mapping[value] for value in group]
    result.update(
        contract="fixed-editorial-input/2",
        shared_input_digest=digest(packet.model_dump(mode="json")),
    )
    return FixedEditorialInput.model_validate(result).model_dump(mode="json")


def add_card(data, value):
    packet = EditorialInput.model_validate(data)
    if packet.contract != "editorial-input/2" or "card_id" in value:
        raise ContractError("editorial_new_card_requires_v2_unassigned_card")
    card = KnowledgeCard.model_validate(dict(value, card_id=new_card_id(set(catalog(packet)))))
    result = packet.model_dump(mode="json")
    result["dossier"]["knowledge_cards"].append(card.model_dump(mode="json"))
    return EditorialInput.model_validate(result).model_dump(mode="json"), card.card_id


def rebind_fixed(data, previous, shared):
    fixed = FixedEditorialInput.model_validate(data)
    before, packet = EditorialInput.model_validate(previous), EditorialInput.model_validate(shared)
    if (
        fixed.contract != "fixed-editorial-input/2"
        or before.contract != "editorial-input/2"
        or packet.contract != "editorial-input/2"
        or fixed.assignment_key != packet.assignment.key
        or fixed.shared_input_digest != digest(before.model_dump(mode="json"))
    ):
        raise ContractError("editorial_fixed_rebind_requires_matching_previous_v2")

    def meaning(value):
        normalized = value.model_dump(mode="json")
        cards = normalized["dossier"]["knowledge_cards"]
        cards.sort(key=lambda card: card["card_id"])
        for card in cards:
            card.pop("label")
            card.pop("title")
        return normalized

    if meaning(before) != meaning(packet):
        raise ContractError("editorial_fixed_rebind_only_card_names_and_order")
    fixed.plan.validate_against(
        material_locators=catalog(packet),
        speaker_ids=[speaker.label for speaker in packet.assignment.speakers],
    )
    result = fixed.model_dump(mode="json")
    result["shared_input_digest"] = digest(packet.model_dump(mode="json"))
    return result
