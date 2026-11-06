"""Card identity belongs to the stored packet, independently of titles, order and source spans."""


def card_id(packet, card):
    return card.card_id if packet.contract == "editorial-input/2" else card.label


def catalog(packet):
    return [card_id(packet, card) for card in packet.dossier.knowledge_cards]
