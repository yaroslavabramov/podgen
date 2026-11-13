"""Card identity belongs to the stored packet, independently of titles, order and source spans."""


def card_id(packet, card):
    return card.card_id if packet.contract == "editorial-input/2" else card.label


def catalog(packet):
    if getattr(packet, "contract", None) == "research-editorial-input/1":
        return [row.material_id for row in packet.sections]
    return [card_id(packet, card) for card in packet.dossier.knowledge_cards]
